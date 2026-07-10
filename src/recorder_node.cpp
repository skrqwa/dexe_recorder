// ----------------------------------------------------------------------------
// Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#include "dexe_recorder/recorder_node.h"

#include <nlohmann/json.hpp>
#include <yaml-cpp/yaml.h>
#include <ament_index_cpp/get_package_share_directory.hpp>
#include <sys/stat.h>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <filesystem>

namespace dexe_recorder {

using json = nlohmann::ordered_json;
using std::placeholders::_1;
using std::placeholders::_2;

// ============================ RecorderConfig ============================

RecorderConfig RecorderConfig::Load(const std::string& path) {
  RecorderConfig cfg;
  try {
    YAML::Node node = YAML::LoadFile(path);
    if (auto v = node["state_topic"]) cfg.state_topic = v.as<std::string>();
    if (auto v = node["head_compressed_topic"]) cfg.head_compressed_topic = v.as<std::string>();
    if (auto v = node["hand_left_topic"]) cfg.hand_left_topic = v.as<std::string>();
    if (auto v = node["hand_right_topic"]) cfg.hand_right_topic = v.as<std::string>();
    if (auto v = node["ee_left_topic"]) cfg.ee_left_topic = v.as<std::string>();
    if (auto v = node["ee_right_topic"]) cfg.ee_right_topic = v.as<std::string>();
    if (auto v = node["output_dir"]) cfg.output_dir = v.as<std::string>();
    if (auto v = node["save_format"]) cfg.save_format = v.as<std::string>();
    if (auto v = node["max_buffer_frames"]) cfg.max_buffer_frames = v.as<int>();
    if (auto v = node["max_segment_duration_sec"]) cfg.max_segment_duration_sec = v.as<double>();
  } catch (const std::exception& e) {
    RCLCPP_WARN(rclcpp::get_logger("dexe_recorder"), "Config load failed (%s), using defaults", e.what());
  }
  return cfg;
}

// ============================ RecorderNode ============================

RecorderNode::RecorderNode(const rclcpp::NodeOptions& options) : rclcpp::Node("dexe_recorder", options) {
  // 加载配置优先级：参数指定 > ~/workspace/dexe_recorder/config > 包 share 目录
  std::string cfg_path = this->declare_parameter<std::string>("config", "");
  if (cfg_path.empty()) {
    // 优先查找 workspace/dexe_recorder/config（傻瓜式部署目录）
    std::string home_cfg = std::string(std::getenv("HOME")) +
                           "/workspace/dexe_recorder/config/auto_recorder.yaml";
    if (std::filesystem::exists(home_cfg)) {
      cfg_path = home_cfg;
    } else {
      // 回退到包 share 目录（ament_index 自动解析）
      std::string pkg_dir = ament_index_cpp::get_package_share_directory("dexe_recorder");
      std::string default_cfg = pkg_dir + "/config/auto_recorder.yaml";
      if (std::filesystem::exists(default_cfg)) {
        cfg_path = default_cfg;
      }
    }
  }
  if (!cfg_path.empty()) {
    config_ = RecorderConfig::Load(cfg_path);
    RCLCPP_INFO(this->get_logger(), "Loaded config from %s (save_format=%s)",
                cfg_path.c_str(), config_.save_format.c_str());
  } else {
    RCLCPP_INFO(this->get_logger(), "No config loaded, using defaults (save_format=%s)",
                config_.save_format.c_str());
  }

  // 创建输出目录
  std::filesystem::create_directories(config_.output_dir);

  // 订阅话题
  rclcpp::QoS qos = rclcpp::QoS(10).best_effort();
  state_sub_ = this->create_subscription<std_msgs::msg::String>(
      config_.state_topic, qos, std::bind(&RecorderNode::OnState, this, _1));

  // 头部相机：订阅 compressed（实测 resize 多订阅者降频，compressed 不降）
  head_compressed_sub_ = this->create_subscription<sensor_msgs::msg::CompressedImage>(
      config_.head_compressed_topic, qos,
      std::bind(&RecorderNode::OnCompressedImage, this, _1));

  // 手部相机：订阅 raw Image（PC2 本地，无跨机问题）
  image_subs_.push_back(this->create_subscription<sensor_msgs::msg::Image>(
      config_.hand_left_topic, qos,
      [this](const sensor_msgs::msg::Image::ConstSharedPtr msg) { OnImage("hand_left", msg); }));
  image_subs_.push_back(this->create_subscription<sensor_msgs::msg::Image>(
      config_.hand_right_topic, qos,
      [this](const sensor_msgs::msg::Image::ConstSharedPtr msg) { OnImage("hand_right", msg); }));

  ee_left_sub_ = this->create_subscription<end_effector_interfaces::msg::EEJointControl>(
      config_.ee_left_topic, qos,
      [this](const end_effector_interfaces::msg::EEJointControl::ConstSharedPtr msg) { OnEndEffector("left", msg); });
  ee_right_sub_ = this->create_subscription<end_effector_interfaces::msg::EEJointControl>(
      config_.ee_right_topic, qos,
      [this](const end_effector_interfaces::msg::EEJointControl::ConstSharedPtr msg) { OnEndEffector("right", msg); });

  // 服务
  start_srv_ = this->create_service<std_srvs::srv::Trigger>(
      "~/start_recording", std::bind(&RecorderNode::HandleStart, this, _1, _2));
  stop_srv_ = this->create_service<std_srvs::srv::Trigger>(
      "~/stop_recording", std::bind(&RecorderNode::HandleStop, this, _1, _2));
  status_srv_ = this->create_service<std_srvs::srv::Trigger>(
      "~/get_status", std::bind(&RecorderNode::HandleStatus, this, _1, _2));

  RCLCPP_INFO(this->get_logger(), "dexe_recorder ready. state_topic=%s", config_.state_topic.c_str());
}

RecorderNode::~RecorderNode() {
  if (recording_.load()) {
    StopRecording();
  }
}

bool RecorderNode::StartRecording() {
  if (recording_.load()) {
    RCLCPP_WARN(this->get_logger(), "Already recording");
    return false;
  }

  current_session_ = MakeSessionId();
  current_session_dir_ = config_.output_dir + "/" + current_session_;
  std::filesystem::create_directories(current_session_dir_);

  // 打开 pose_record 临时 JSONL 文件（stop 时合并为 JSON）
  std::string pose_path = current_session_dir_ + "/pose_record_" + current_session_ + ".jsonl.tmp";
  pose_file_.open(pose_path, std::ios::out | std::ios::trunc);
  if (!pose_file_.is_open()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to open %s", pose_path.c_str());
    return false;
  }

  // 打开 metadata.jsonl
  std::string meta_path = current_session_dir_ + "/metadata.jsonl";
  metadata_file_.open(meta_path, std::ios::out | std::ios::trunc);
  if (!metadata_file_.is_open()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to open %s", meta_path.c_str());
    pose_file_.close();
    return false;
  }

  // 创建相机目录（图片模式时 GStreamer 会用到）
  for (const auto& sub : {"head/left", "head/right", "hand/left", "hand/right"}) {
    std::filesystem::create_directories(current_session_dir_ + "/" + sub);
  }

  // 初始化 GStreamer 录制器
  auto fmt = (config_.save_format == "jpeg") ? GstRecorder::Format::JPEG : GstRecorder::Format::VIDEO;
  gst_recorder_ = std::make_unique<GstRecorder>();
  if (!gst_recorder_->Start(current_session_dir_, fmt)) {
    RCLCPP_ERROR(this->get_logger(), "Failed to start GStreamer recorder");
    pose_file_.close();
    metadata_file_.close();
    gst_recorder_.reset();
    return false;
  }

  frame_counter_.store(0);
  image_frame_counter_.store(0);
  head_left_counter_.store(0);
  head_right_counter_.store(0);
  hand_left_counter_.store(0);
  hand_right_counter_.store(0);
  session_start_time_ = this->now().seconds();
  pose_frames_.clear();
  {
    std::lock_guard<std::mutex> lk(latest_state_mtx_);
    latest_ee_values_.clear();
  }
  buffer_ = std::make_unique<FrameBuffer>(config_.max_buffer_frames);
  recording_.store(true);
  writer_thread_ = std::thread(&RecorderNode::WriterLoop, this);

  RCLCPP_INFO(this->get_logger(), "Recording STARTED: session=%s, dir=%s", current_session_.c_str(),
              current_session_dir_.c_str());
  return true;
}

bool RecorderNode::StopRecording() {
  if (!recording_.load()) {
    RCLCPP_WARN(this->get_logger(), "Not recording");
    return false;
  }

  recording_.store(false);
  buffer_->Stop();
  if (writer_thread_.joinable()) {
    writer_thread_.join();
  }

  session_end_time_ = this->now().seconds();

  // 停止 GStreamer 录制（flush pipeline + 写文件）
  if (gst_recorder_) {
    gst_recorder_->Stop();
    gst_recorder_.reset();
  }

  if (pose_file_.is_open()) {
    pose_file_.close();
  }
  if (metadata_file_.is_open()) {
    metadata_file_.close();
  }

  // 合并临时 JSONL 为 tele 兼容的 JSON 格式
  FinalizePoseRecord();

  RCLCPP_INFO(this->get_logger(), "Recording STOPPED: session=%s, frames=%lu, dropped=%lu",
              current_session_.c_str(), frame_counter_.load(), buffer_->dropped_count());
  buffer_.reset();
  return true;
}

void RecorderNode::HandleStart(const std::shared_ptr<std_srvs::srv::Trigger::Request> /*req*/,
                                std::shared_ptr<std_srvs::srv::Trigger::Response> res) {
  bool ok = StartRecording();
  res->success = ok;
  res->message = ok ? ("session=" + current_session_) : "already recording or failed";
}

void RecorderNode::HandleStop(const std::shared_ptr<std_srvs::srv::Trigger::Request> /*req*/,
                               std::shared_ptr<std_srvs::srv::Trigger::Response> res) {
  bool ok = StopRecording();
  res->success = ok;
  res->message = ok ? ("session=" + current_session_) : "not recording";
}

void RecorderNode::HandleStatus(const std::shared_ptr<std_srvs::srv::Trigger::Request> /*req*/,
                                std::shared_ptr<std_srvs::srv::Trigger::Response> res) {
  res->success = recording_.load();
  res->message = recording_.load()
                     ? ("recording session=" + current_session_ + " frames=" + std::to_string(frame_counter_.load()))
                     : "idle";
}

void RecorderNode::OnState(const std_msgs::msg::String::ConstSharedPtr msg) {
  if (!recording_.load()) {
    return;
  }

  Frame frame;
  frame.timestamp = this->now().seconds();
  if (!ParseStateJson(msg->data, &frame)) {
    return;
  }

  // 缓存最新关节状态（供相机/EE 帧合并用）
  {
    std::lock_guard<std::mutex> lk(latest_state_mtx_);
    latest_state_frame_ = frame;
  }

  // 合并缓存的相机/EE 数据
  // Phase 1: 暂不合并，直接入队（关节帧为主帧）
  buffer_->Push(std::move(frame));
}

void RecorderNode::OnCompressedImage(const sensor_msgs::msg::CompressedImage::ConstSharedPtr msg) {
  if (!recording_.load() || !gst_recorder_) {
    return;
  }
  // 推入 GStreamer pipeline（nvjpegdec 硬解 -> videocrop 拆分 -> 编码保存）
  gst_recorder_->PushCompressedFrame(msg->data.data(), msg->data.size());

  // 写 metadata.jsonl：左右目各自独立连续下标（与 tele 一致）
  double ts = this->now().seconds();
  uint64_t fid = image_frame_counter_.fetch_add(1);
  uint64_t left_idx = head_left_counter_.fetch_add(1);
  uint64_t right_idx = head_right_counter_.fetch_add(1);

  char left_name[32], right_name[32];
  std::snprintf(left_name, sizeof(left_name), "%06lu", left_idx);
  std::snprintf(right_name, sizeof(right_name), "%06lu", right_idx);

  json meta_left;
  meta_left["timestamp"] = ts;
  meta_left["frame_id"] = fid;
  meta_left["camera_type"] = "head_left";
  meta_left["ros_timestamp"] = msg->header.stamp.sec + msg->header.stamp.nanosec / 1e9;
  meta_left["image_path"] = std::string("head/left/") + left_name + ".jpg";

  json meta_right;
  meta_right["timestamp"] = ts;
  meta_right["frame_id"] = fid;
  meta_right["camera_type"] = "head_right";
  meta_right["ros_timestamp"] = msg->header.stamp.sec + msg->header.stamp.nanosec / 1e9;
  meta_right["image_path"] = std::string("head/right/") + right_name + ".jpg";

  {
    std::lock_guard<std::mutex> lk(image_mtx_);
    if (metadata_file_.is_open()) {
      metadata_file_ << meta_left.dump() << "\n";
      metadata_file_ << meta_right.dump() << "\n";
      metadata_file_.flush();
    }
  }
}

void RecorderNode::OnImage(const std::string& camera_name, const sensor_msgs::msg::Image::ConstSharedPtr msg) {
  if (!recording_.load() || !gst_recorder_) {
    return;
  }
  // 手部相机 raw Image 推入 GStreamer pipeline
  gst_recorder_->PushHandFrame(camera_name, msg->data.data(), msg->data.size(),
                                msg->width, msg->height, msg->encoding);

  // 写 metadata.jsonl：各自独立连续下标
  double ts = this->now().seconds();
  uint64_t fid = image_frame_counter_.fetch_add(1);
  uint64_t idx;
  std::string path_prefix;
  if (camera_name == "hand_left") {
    idx = hand_left_counter_.fetch_add(1);
    path_prefix = "hand/left/";
  } else {
    idx = hand_right_counter_.fetch_add(1);
    path_prefix = "hand/right/";
  }
  char name[32];
  std::snprintf(name, sizeof(name), "%06lu", idx);

  json meta;
  meta["timestamp"] = ts;
  meta["frame_id"] = fid;
  meta["camera_type"] = camera_name;
  meta["ros_timestamp"] = msg->header.stamp.sec + msg->header.stamp.nanosec / 1e9;
  meta["image_path"] = path_prefix + name + ".jpg";
  {
    std::lock_guard<std::mutex> lk(image_mtx_);
    if (metadata_file_.is_open()) {
      metadata_file_ << meta.dump() << "\n";
      metadata_file_.flush();
    }
  }
}

void RecorderNode::OnEndEffector(const std::string& side,
                                  const end_effector_interfaces::msg::EEJointControl::ConstSharedPtr msg) {
  if (!recording_.load()) {
    return;
  }
  // 缓存最新 EE 数据（供 WriteFrame 合并到 pose JSON）
  // 遥操命名：LEFT_HAND_THUMB1, LEFT_HAND_THUMB2, ... LEFT_GRIPPER
  // EE 话题的 joint_names 可能是 THUMB1/THUMB2/... 或 GRIPPER
  // 映射规则：LEFT_/RIGHT_ + HAND_ + joint_name（GRIPPER 例外，不加 HAND_）
  std::string prefix = (side == "left") ? "LEFT_" : "RIGHT_";
  {
    std::lock_guard<std::mutex> lk(latest_state_mtx_);
    for (size_t i = 0; i < msg->joint_names.size() && i < msg->values.size(); ++i) {
      const std::string& jn = msg->joint_names[i];
      if (jn.find("GRIPPER") != std::string::npos) {
        latest_ee_values_[prefix + jn] = msg->values[i];
      } else {
        latest_ee_values_[prefix + "HAND_" + jn] = msg->values[i];
      }
    }
  }
}

void RecorderNode::WriterLoop() {
  Frame frame;
  while (buffer_->Pop(&frame)) {
    WriteFrame(frame);
    frame_counter_.fetch_add(1);
  }
  RCLCPP_INFO(this->get_logger(), "Writer loop exited");
}

std::string RecorderNode::MakeSessionId() const {
  std::time_t now = std::time(nullptr);
  std::tm tm{};
  localtime_r(&now, &tm);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y%m%d_%H%M%S", &tm);
  return std::string(buf);
}

bool RecorderNode::ParseStateJson(const std::string& json_str, Frame* frame) {
  try {
    json j = json::parse(json_str);

    // timestamp（纳秒 → 秒）
    if (j.contains("timestamp")) {
      uint64_t ts_ns = j["timestamp"].get<uint64_t>();
      frame->timestamp = static_cast<double>(ts_ns) / 1e9;
    }

    // joint_name
    if (j.contains("joint_name")) {
      frame->joint_names = j["joint_name"].get<std::vector<std::string>>();
    }
    // 关节反馈
    if (j.contains("joint_position")) frame->joint_position = j["joint_position"].get<std::vector<double>>();
    if (j.contains("joint_velocity")) frame->joint_velocity = j["joint_velocity"].get<std::vector<double>>();
    if (j.contains("joint_torque")) frame->joint_torque = j["joint_torque"].get<std::vector<double>>();
    // 关节指令
    if (j.contains("joint_position_cmd")) frame->joint_position_cmd = j["joint_position_cmd"].get<std::vector<double>>();
    if (j.contains("joint_velocity_cmd")) frame->joint_velocity_cmd = j["joint_velocity_cmd"].get<std::vector<double>>();
    if (j.contains("joint_torque_cmd")) frame->joint_torque_cmd = j["joint_torque_cmd"].get<std::vector<double>>();

    return true;
  } catch (const std::exception& e) {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000, "JSON parse error: %s", e.what());
    return false;
  }
}

void RecorderNode::WriteFrame(const Frame& frame) {
  // 缓存帧到内存（stop 时合并为 tele 兼容 JSON）
  // 遥操兼容的固定关节顺序（35 个字段）
  // 关节位置（从 robot_server_state 的 joint_name + joint_position）
  std::map<std::string, double> joint_map;
  if (!frame.joint_position.empty()) {
    for (size_t i = 0; i < frame.joint_names.size() && i < frame.joint_position.size(); ++i) {
      joint_map[frame.joint_names[i]] = frame.joint_position[i];
    }
  }

  // 合并缓存的 EE 数据
  {
    std::lock_guard<std::mutex> lk(latest_state_mtx_);
    for (const auto& [name, val] : latest_ee_values_) {
      joint_map[name] = val;
    }
  }

  // 按遥操顺序输出（缺失的补 0.0）
  static const std::vector<std::string> kTeleopOrder = {
      "ANKLE", "KNEE", "BUTTOCK", "WAIST", "NECK1", "NECK2",
      "LEFT_J1", "LEFT_J2", "LEFT_J3", "LEFT_J4", "LEFT_J5", "LEFT_J6", "LEFT_J7",
      "RIGHT_J1", "RIGHT_J2", "RIGHT_J3", "RIGHT_J4", "RIGHT_J5", "RIGHT_J6", "RIGHT_J7",
      "LEFT_HAND_THUMB1", "LEFT_HAND_THUMB2", "LEFT_HAND_INDEX", "LEFT_HAND_MIDDLE",
      "LEFT_HAND_RING", "LEFT_HAND_PINKY",
      "RIGHT_HAND_THUMB1", "RIGHT_HAND_THUMB2", "RIGHT_HAND_INDEX", "RIGHT_HAND_MIDDLE",
      "RIGHT_HAND_RING", "RIGHT_HAND_PINKY",
      "LEFT_GRIPPER", "RIGHT_GRIPPER"
  };

  // 用 ordered_json 保持遥操的关节顺序
  nlohmann::ordered_json data;
  for (const auto& name : kTeleopOrder) {
    auto it = joint_map.find(name);
    data[name] = (it != joint_map.end()) ? it->second : 0.0;
  }

  nlohmann::ordered_json j;
  j["frame_id"] = frame_counter_.load();
  j["timestamp"] = frame.timestamp;
  j["data"] = data;

  {
    std::lock_guard<std::mutex> lk(image_mtx_);
    pose_frames_.push_back(j.dump(2));  // 缩进 2 空格，与遥操格式一致
  }

  // 同时写临时 JSONL（备份，stop 时合并）
  if (pose_file_.is_open()) {
    pose_file_ << j.dump() << "\n";
    pose_file_.flush();
  }
}

std::string RecorderNode::ImagePath(const std::string& camera_type, uint64_t frame_id) const {
  // camera_type: "head_left" -> "head/left/000123.jpg"
  std::string dir;
  if (camera_type == "head_left") dir = "head/left";
  else if (camera_type == "head_right") dir = "head/right";
  else if (camera_type == "hand_left") dir = "hand/left";
  else if (camera_type == "hand_right") dir = "hand/right";
  else dir = "other";

  char seq[16];
  std::snprintf(seq, sizeof(seq), "%06lu", frame_id);
  return dir + "/" + std::string(seq) + ".jpg";
}

void RecorderNode::FinalizePoseRecord() {
  // 合并为 tele 兼容的 pose_record_<sid>.json
  std::string json_path = current_session_dir_ + "/pose_record_" + current_session_ + ".json";
  std::ofstream out(json_path, std::ios::out | std::ios::trunc);
  if (!out.is_open()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to write %s", json_path.c_str());
    return;
  }

  json root;
  root["session_id"] = current_session_;
  root["start_time"] = session_start_time_;
  root["end_time"] = session_end_time_;
  root["duration"] = session_end_time_ - session_start_time_;

  // 手动构建 JSON 保持帧的顺序（pose_frames_ 存的是 ordered_json dump(2) 的字符串）
  // 每帧缩进 4 空格（帧内已有 2 空格缩进，额外加 4 空格 -> 6 空格，与遥操格式一致）
  std::string frames_str = "[\n";
  {
    std::lock_guard<std::mutex> lk(image_mtx_);
    root["frame_count"] = pose_frames_.size();
    root["metadata"] = json::object();
    for (size_t i = 0; i < pose_frames_.size(); ++i) {
      if (i > 0) frames_str += ",\n";
      // 给每行加 4 空格前缀
      std::string frame = pose_frames_[i];
      std::string indented;
      size_t pos = 0;
      while (pos < frame.size()) {
        size_t nl = frame.find('\n', pos);
        if (nl == std::string::npos) {
          indented += "    " + frame.substr(pos);
          break;
        }
        indented += "    " + frame.substr(pos, nl - pos) + "\n";
        pos = nl + 1;
      }
      frames_str += indented;
    }
  }
  frames_str += "\n  ]";

  // 输出完整 JSON（root 部分用 nlohmann::json，frames 部分手动拼接保持顺序）
  out << "{\n";
  out << "  \"session_id\": " << root["session_id"].dump() << ",\n";
  out << "  \"start_time\": " << root["start_time"].dump() << ",\n";
  out << "  \"end_time\": " << root["end_time"].dump() << ",\n";
  out << "  \"duration\": " << root["duration"].dump() << ",\n";
  out << "  \"frame_count\": " << root["frame_count"].dump() << ",\n";
  out << "  \"metadata\": " << root["metadata"].dump() << ",\n";
  out << "  \"frames\": " << frames_str << "\n";
  out << "}";
  out.close();

  // 删除临时 JSONL
  std::string tmp_path = current_session_dir_ + "/pose_record_" + current_session_ + ".jsonl.tmp";
  std::filesystem::remove(tmp_path);

  RCLCPP_INFO(this->get_logger(), "pose_record.json written: %s (%lu frames)", json_path.c_str(),
              root["frame_count"].get<size_t>());
}

}  // namespace dexe_recorder
