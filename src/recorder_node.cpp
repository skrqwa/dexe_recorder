// ----------------------------------------------------------------------------
// Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#include "dexe_recorder/recorder_node.h"

#include <nlohmann/json.hpp>
#include <yaml-cpp/yaml.h>
#include <ament_index_cpp/get_package_share_directory.hpp>
#include <algorithm>
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

  ee_left_sub_ = this->create_subscription<end_effector_interfaces::msg::EEFeedback>(
      config_.ee_left_topic, qos,
      [this](const end_effector_interfaces::msg::EEFeedback::ConstSharedPtr msg) { OnEndEffector("left", msg); });
  ee_right_sub_ = this->create_subscription<end_effector_interfaces::msg::EEFeedback>(
      config_.ee_right_topic, qos,
      [this](const end_effector_interfaces::msg::EEFeedback::ConstSharedPtr msg) { OnEndEffector("right", msg); });

  // EE 命令订阅（录 pose_record 用）
  ee_cmd_left_sub_ = this->create_subscription<end_effector_interfaces::msg::EEJointControl>(
      "/control/ee/left", qos,
      [this](const end_effector_interfaces::msg::EEJointControl::ConstSharedPtr msg) { OnEeCommand("left", msg); });
  ee_cmd_right_sub_ = this->create_subscription<end_effector_interfaces::msg::EEJointControl>(
      "/control/ee/right", qos,
      [this](const end_effector_interfaces::msg::EEJointControl::ConstSharedPtr msg) { OnEeCommand("right", msg); });

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
    latest_ee_cmd_values_.clear();
    ee_left_name_.clear();
    ee_right_name_.clear();
  }

  // 打开 feedback_record 临时 JSONL 文件（stop 时合并为 JSON）
  std::string feedback_path = current_session_dir_ + "/feedback_record_" + current_session_ + ".jsonl.tmp";
  feedback_file_.open(feedback_path, std::ios::out | std::ios::trunc);
  if (!feedback_file_.is_open()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to open %s", feedback_path.c_str());
  }

  feedback_frames_.clear();
  feedback_frame_counter_.store(0);

  recording_.store(true);
  writer_thread_ = std::thread(&RecorderNode::WriterLoop, this);

  // 启动反馈录制写线程（30Hz）
  feedback_recording_.store(true);
  feedback_writer_thread_ = std::thread(&RecorderNode::FeedbackWriterLoop, this);

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
  if (writer_thread_.joinable()) {
    writer_thread_.join();
  }

  // 停止反馈录制写线程
  feedback_recording_.store(false);
  if (feedback_writer_thread_.joinable()) {
    feedback_writer_thread_.join();
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
  if (feedback_file_.is_open()) {
    feedback_file_.close();
  }
  if (metadata_file_.is_open()) {
    metadata_file_.close();
  }

  // 合并临时 JSONL 为 tele 兼容的 JSON 格式
  FinalizePoseRecord();
  FinalizeFeedbackRecord();

  RCLCPP_INFO(this->get_logger(), "Recording STOPPED: session=%s, frames=%lu",
              current_session_.c_str(), frame_counter_.load());
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

  // 缓存最新关节状态（供 30Hz 写线程取快照）
  {
    std::lock_guard<std::mutex> lk(latest_state_mtx_);
    latest_state_frame_ = frame;
  }
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
                                  const end_effector_interfaces::msg::EEFeedback::ConstSharedPtr msg) {
  if (!recording_.load()) {
    return;
  }
  // 缓存 ee_name 和关节反馈数据（供 WriteFrame 合并到 pose JSON）
  std::string ee_name = msg->ee_name;
  {
    std::lock_guard<std::mutex> lk(latest_state_mtx_);
    if (side == "left") {
      ee_left_name_ = ee_name;
    } else {
      ee_right_name_ = ee_name;
    }
    // 关节名映射（与遥操 ee_hand_mapping.py 规则一致）
    for (const auto& js : msg->joint_states) {
      std::string mapped = MapEeJointName(side, ee_name, js.name);
      latest_ee_values_[mapped] = js.position;
    }
  }
}

std::string RecorderNode::MapEeJointName(const std::string& side, const std::string& ee_name,
                                          const std::string& joint_name) const {
  // 与遥操 ee_hand_mapping.py 的 get_recording_finger_names 规则一致
  std::string prefix = (side == "left") ? "LEFT_" : "RIGHT_";

  // 强脑巧手默认映射：T_MCP -> HAND_THUMB1 等
  // BrainCo_Revo1_R / BrainCo_Revo1_E
  if (ee_name == "BrainCo_Revo1_R" || ee_name == "BrainCo_Revo1_E") {
    // SIX_DOF_EE_JOINT_NAMES 顺序: T_MCP, T_CMC_YAW, IF_MCP_PITCH, MF_MCP_PITCH, RF_MCP_PITCH, LF_MCP_PITCH
    // 映射到 DEFAULT_HAND_CONTROL_JOINT_NAMES: HAND_THUMB1, HAND_THUMB2, HAND_INDEX, HAND_MIDDLE, HAND_RING, HAND_PINKY
    static const std::map<std::string, std::string> brainco_map = {
        {"T_MCP", "HAND_THUMB1"},
        {"T_CMC_YAW", "HAND_THUMB2"},
        {"IF_MCP_PITCH", "HAND_INDEX"},
        {"MF_MCP_PITCH", "HAND_MIDDLE"},
        {"RF_MCP_PITCH", "HAND_RING"},
        {"LF_MCP_PITCH", "HAND_PINKY"},
    };
    auto it = brainco_map.find(joint_name);
    if (it != brainco_map.end()) {
      return prefix + it->second;
    }
  }

  // 夹爪：统一映射为 GRIPPER
  if (ee_name == "DH_PGC_140_50" || ee_name == "DH_AG_160_95" || ee_name == "Piper_Gripper") {
    return prefix + "GRIPPER";
  }

  // 其他灵巧手（Linker_L6, Linker_L20, PaXini_Dex_H13, DexHand_021S）：直接加前缀
  return prefix + joint_name;
}

void RecorderNode::OnEeCommand(const std::string& side,
                                const end_effector_interfaces::msg::EEJointControl::ConstSharedPtr msg) {
  if (!recording_.load()) {
    return;
  }
  // 缓存最新 EE 命令数据（供 pose_record 用）
  // EEJointControl 的 name + value 对应关节命令
  std::string ee_name;
  {
    std::lock_guard<std::mutex> lk(latest_state_mtx_);
    ee_name = (side == "left") ? ee_left_name_ : ee_right_name_;
  }
  // EE 命令的关节名和反馈一样需要映射
  // 但 EEJointControl 的 name 字段可能和 EEFeedback 的 joint_states[].name 不同
  // ACT 发的 name 是 hand_joint_name（配置参数），通常是 "THUMB1" 等旧版名
  // 这里用 name + 前缀直接存储，不做映射（命令侧用原始名）
  std::string prefix = (side == "left") ? "LEFT_" : "RIGHT_";
  {
    std::lock_guard<std::mutex> lk(latest_state_mtx_);
    for (size_t i = 0; i < msg->joint_names.size() && i < msg->values.size(); ++i) {
      latest_ee_cmd_values_[prefix + msg->joint_names[i]] = msg->values[i];
    }
  }
}

void RecorderNode::WriterLoop() {
  // 30Hz 墙钟驱动：取最新命令快照，组装帧写盘
  const double interval = 1.0 / 30.0;
  auto next_t = std::chrono::steady_clock::now() + std::chrono::milliseconds(33);

  while (recording_.load()) {
    std::this_thread::sleep_until(next_t);
    next_t += std::chrono::microseconds(static_cast<int64_t>(interval * 1e6));

    // 跳帧追齐（被挂起后恢复）
    auto now = std::chrono::steady_clock::now();
    if (next_t < now) {
      next_t = now + std::chrono::microseconds(static_cast<int64_t>(interval * 1e6));
    }

    // 取最新命令快照
    Frame frame;
    frame.timestamp = this->now().seconds();
    {
      std::lock_guard<std::mutex> lk(latest_state_mtx_);
      frame = latest_state_frame_;
    }
    WriteFrame(frame);
    frame_counter_.fetch_add(1);
  }
  RCLCPP_INFO(this->get_logger(), "Writer loop exited");
}

void RecorderNode::FeedbackWriterLoop() {
  // 30Hz 独立写线程：取最新反馈快照，组装帧，流式写盘
  const double interval = 1.0 / 30.0;  // 33ms
  auto next_t = std::chrono::steady_clock::now() + std::chrono::milliseconds(33);

  while (feedback_recording_.load()) {
    std::this_thread::sleep_until(next_t);
    next_t += std::chrono::microseconds(static_cast<int64_t>(interval * 1e6));

    // 跳帧追齐（被挂起后恢复）
    auto now = std::chrono::steady_clock::now();
    if (next_t < now) {
      next_t = now + std::chrono::microseconds(static_cast<int64_t>(interval * 1e6));
    }

    // 取最新反馈快照
    std::map<std::string, double> joint_map;
    std::map<std::string, double> ee_map;
    double ts = this->now().seconds();
    {
      std::lock_guard<std::mutex> lk(latest_state_mtx_);
      // 关节反馈
      if (!latest_state_frame_.joint_position.empty()) {
        for (size_t i = 0; i < latest_state_frame_.joint_names.size() &&
                            i < latest_state_frame_.joint_position.size(); ++i) {
          joint_map[latest_state_frame_.joint_names[i]] = latest_state_frame_.joint_position[i];
        }
      }
      // EE 反馈
      for (const auto& [name, val] : latest_ee_values_) {
        ee_map[name] = val;
      }
    }

    // 组装反馈帧
    nlohmann::ordered_json data;
    static const std::vector<std::string> kArmOrder = {
        "ANKLE", "KNEE", "BUTTOCK", "WAIST", "NECK1", "NECK2",
        "LEFT_J1", "LEFT_J2", "LEFT_J3", "LEFT_J4", "LEFT_J5", "LEFT_J6", "LEFT_J7",
        "RIGHT_J1", "RIGHT_J2", "RIGHT_J3", "RIGHT_J4", "RIGHT_J5", "RIGHT_J6", "RIGHT_J7",
    };
    for (const auto& name : kArmOrder) {
      auto it = joint_map.find(name);
      data[name] = (it != joint_map.end()) ? it->second : 0.0;
    }
    for (const auto& [name, val] : joint_map) {
      if (std::find(kArmOrder.begin(), kArmOrder.end(), name) == kArmOrder.end()) {
        data[name] = val;
      }
    }
    // EE 反馈关节
    for (const auto& [name, val] : ee_map) {
      data[name] = val;
    }

    nlohmann::ordered_json j;
    j["frame_id"] = feedback_frame_counter_.fetch_add(1);
    j["timestamp"] = ts;
    j["data"] = data;

    // 流式写盘
    std::string frame_str = j.dump(2);
    {
      std::lock_guard<std::mutex> lk(image_mtx_);
      feedback_frames_.push_back(frame_str);
    }
    if (feedback_file_.is_open()) {
      feedback_file_ << j.dump() << "\n";
      feedback_file_.flush();
    }
  }
  RCLCPP_INFO(this->get_logger(), "Feedback writer loop exited");
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
  // pose_record 录制命令数据：joint_position_cmd + EE 命令
  std::map<std::string, double> joint_map;
  if (!frame.joint_position_cmd.empty()) {
    for (size_t i = 0; i < frame.joint_names.size() && i < frame.joint_position_cmd.size(); ++i) {
      joint_map[frame.joint_names[i]] = frame.joint_position_cmd[i];
    }
  }

  // 合并缓存的 EE 命令数据
  {
    std::lock_guard<std::mutex> lk(latest_state_mtx_);
    for (const auto& [name, val] : latest_ee_cmd_values_) {
      joint_map[name] = val;
    }
  }

  // 按遥操顺序输出手臂关节（EE 关节由实际反馈决定，不硬编码占位符）
  static const std::vector<std::string> kArmOrder = {
      "ANKLE", "KNEE", "BUTTOCK", "WAIST", "NECK1", "NECK2",
      "LEFT_J1", "LEFT_J2", "LEFT_J3", "LEFT_J4", "LEFT_J5", "LEFT_J6", "LEFT_J7",
      "RIGHT_J1", "RIGHT_J2", "RIGHT_J3", "RIGHT_J4", "RIGHT_J5", "RIGHT_J6", "RIGHT_J7",
  };

  // 用 ordered_json 保持遥操的关节顺序
  nlohmann::ordered_json data;
  for (const auto& name : kArmOrder) {
    auto it = joint_map.find(name);
    data[name] = (it != joint_map.end()) ? it->second : 0.0;
  }

  // 输出所有 EE 关节（由 MapEeJointName 映射后的实际反馈数据）
  for (const auto& [name, val] : joint_map) {
    if (std::find(kArmOrder.begin(), kArmOrder.end(), name) == kArmOrder.end()) {
      data[name] = val;
    }
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

void RecorderNode::FinalizeFeedbackRecord() {
  // 合并为 feedback_record_<sid>.json（与 pose_record 同级目录）
  std::string json_path = current_session_dir_ + "/feedback_record_" + current_session_ + ".json";
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
  root["target_fps"] = 30.0;

  std::string frames_str = "[\n";
  {
    std::lock_guard<std::mutex> lk(image_mtx_);
    root["frame_count"] = feedback_frames_.size();
    for (size_t i = 0; i < feedback_frames_.size(); ++i) {
      if (i > 0) frames_str += ",\n";
      std::string frame = feedback_frames_[i];
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

  out << "{\n";
  out << "  \"session_id\": " << root["session_id"].dump() << ",\n";
  out << "  \"start_time\": " << root["start_time"].dump() << ",\n";
  out << "  \"end_time\": " << root["end_time"].dump() << ",\n";
  out << "  \"duration\": " << root["duration"].dump() << ",\n";
  out << "  \"target_fps\": " << root["target_fps"].dump() << ",\n";
  out << "  \"frame_count\": " << root["frame_count"].dump() << ",\n";
  out << "  \"frames\": " << frames_str << "\n";
  out << "}";
  out.close();

  // 删除临时 JSONL
  std::string tmp_path = current_session_dir_ + "/feedback_record_" + current_session_ + ".jsonl.tmp";
  std::filesystem::remove(tmp_path);

  RCLCPP_INFO(this->get_logger(), "feedback_record.json written: %s (%lu frames)", json_path.c_str(),
              root["frame_count"].get<size_t>());
}

}  // namespace dexe_recorder
