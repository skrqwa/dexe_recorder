// ----------------------------------------------------------------------------
// Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#pragma once

#include <atomic>
#include <condition_variable>
#include <deque>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "end_effector_interfaces/msg/ee_feedback.hpp"
#include "end_effector_interfaces/msg/ee_joint_control.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/compressed_image.hpp"
#include "std_msgs/msg/string.hpp"
#include "std_srvs/srv/trigger.hpp"
#include <nlohmann/json.hpp>
#include "dexe_recorder/gst_recorder.h"

namespace dexe_recorder {

// 单个数据帧（时间戳 + 各类数据）
struct Frame {
  double timestamp;  // 秒
  // 关节数据（从 robot_server_state JSON 解析）
  std::vector<double> joint_position;       // 反馈
  std::vector<double> joint_position_cmd;   // 指令
  std::vector<double> joint_velocity;
  std::vector<double> joint_velocity_cmd;
  std::vector<double> joint_torque;
  std::vector<double> joint_torque_cmd;
  std::vector<std::string> joint_names;
  // 末端执行器
  std::vector<double> ee_left_qpos;
  std::vector<double> ee_right_qpos;
  // 相机图像（按相机名索引，存原始字节 + 编码格式）
  struct ImageData {
    std::string encoding;  // "jpeg"/"png"/"rgb8"/"bgr8"
    int width;
    int height;
    std::vector<uint8_t> data;
  };
  std::unordered_map<std::string, ImageData> images;
};

// 线程安全的有界帧缓冲队列
class FrameBuffer {
 public:
  explicit FrameBuffer(size_t max_size = 600);  // 默认缓存 600 帧

  // 生产端：非阻塞，满了丢弃最旧帧并计数
  void Push(Frame&& frame);

  // 消费端：阻塞等待，返回 false 表示停止
  bool Pop(Frame* out);

  // 停止并唤醒消费线程
  void Stop();

  // 统计
  size_t dropped_count() const { return dropped_.load(); }
  size_t size() const;

 private:
  mutable std::mutex mtx_;
  std::condition_variable cv_;
  std::deque<Frame> queue_;
  size_t max_size_;
  std::atomic<bool> stopped_{false};
  std::atomic<size_t> dropped_{0};
};

// 录制配置
struct RecorderConfig {
  // 数据源话题
  std::string state_topic = "/feedback/robot_server_state";
  std::string head_compressed_topic = "/camera/kfc_compressed";
  std::string hand_left_topic = "/camera_l/color/image_rect_raw";
  std::string hand_right_topic = "/camera_r/color/image_rect_raw";
  std::string ee_left_topic = "/control/ee/left";
  std::string ee_right_topic = "/control/ee/right";

  // 存储路径
  std::string output_dir = "data/recorded_auto";

  // 保存格式：video 或 jpeg
  std::string save_format = "video";

  // 录制参数
  int max_buffer_frames = 600;
  double max_segment_duration_sec = 0;  // 0 = 不限时

  // 从 yaml 加载
  static RecorderConfig Load(const std::string& path);
};

class RecorderNode : public rclcpp::Node {
 public:
  explicit RecorderNode(const rclcpp::NodeOptions& options = rclcpp::NodeOptions());

  ~RecorderNode() override;

 private:
  // 服务回调
  void HandleStart(const std::shared_ptr<std_srvs::srv::Trigger::Request> req,
                   std::shared_ptr<std_srvs::srv::Trigger::Response> res);
  void HandleStop(const std::shared_ptr<std_srvs::srv::Trigger::Request> req,
                 std::shared_ptr<std_srvs::srv::Trigger::Response> res);
  void HandleStatus(const std::shared_ptr<std_srvs::srv::Trigger::Request> req,
                    std::shared_ptr<std_srvs::srv::Trigger::Response> res);

  // 话题回调
  void OnState(const std_msgs::msg::String::ConstSharedPtr msg);
  void OnImage(const std::string& camera_name, const sensor_msgs::msg::Image::ConstSharedPtr msg);
  void OnCompressedImage(const sensor_msgs::msg::CompressedImage::ConstSharedPtr msg);
  void OnEndEffector(const std::string& side, const end_effector_interfaces::msg::EEFeedback::ConstSharedPtr msg);
  void OnEeCommand(const std::string& side, const end_effector_interfaces::msg::EEJointControl::ConstSharedPtr msg);

  // 写盘线程
  void WriterLoop();
  void FeedbackWriterLoop();

  // 启动/停止录制会话
  bool StartRecording();
  bool StopRecording();

  // 辅助
  std::string MakeSessionId() const;
  bool ParseStateJson(const std::string& json_str, Frame* frame);
  void WriteFrame(const Frame& frame);
  void FinalizePoseRecord();
  std::string ImagePath(const std::string& camera_type, uint64_t frame_id) const;

  // EE 关节名映射（与遥操 ee_hand_mapping.py 保持一致）
  std::string MapEeJointName(const std::string& side, const std::string& ee_name,
                              const std::string& joint_name) const;
  void FinalizeFeedbackRecord();

  RecorderConfig config_;

  // ROS 接口
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr state_sub_;
  rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr head_compressed_sub_;
  std::vector<rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr> image_subs_;
  rclcpp::Subscription<end_effector_interfaces::msg::EEFeedback>::SharedPtr ee_left_sub_;
  rclcpp::Subscription<end_effector_interfaces::msg::EEFeedback>::SharedPtr ee_right_sub_;
  rclcpp::Subscription<end_effector_interfaces::msg::EEJointControl>::SharedPtr ee_cmd_left_sub_;
  rclcpp::Subscription<end_effector_interfaces::msg::EEJointControl>::SharedPtr ee_cmd_right_sub_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr start_srv_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr stop_srv_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr status_srv_;

  // 录制状态
  std::atomic<bool> recording_{false};
  std::string current_session_;
  std::string current_session_dir_;
  std::atomic<uint64_t> frame_counter_{0};
  std::atomic<uint64_t> image_frame_counter_{0};  // 全局图片序号（metadata frame_id）
  std::atomic<uint64_t> head_left_counter_{0};    // 头部左目独立序号
  std::atomic<uint64_t> head_right_counter_{0};   // 头部右目独立序号
  std::atomic<uint64_t> hand_left_counter_{0};    // 手部左目独立序号
  std::atomic<uint64_t> hand_right_counter_{0};   // 手部右目独立序号

  // 最新关节数据缓存（相机/EE 帧到来时合并）
  std::mutex latest_state_mtx_;
  Frame latest_state_frame_;  // 仅 joint 字段有效

  // 最新 EE 反馈数据缓存（供 feedback_record 用）
  std::map<std::string, double> latest_ee_values_;
  std::string ee_left_name_;   // 左手末端型号（从 EEFeedback.ee_name 获取）
  std::string ee_right_name_;  // 右手末端型号

  // 最新 EE 命令缓存（供 pose_record 用）
  std::map<std::string, double> latest_ee_cmd_values_;

  // 反馈录制（30Hz 独立写线程）
  std::ofstream feedback_file_;
  std::thread feedback_writer_thread_;
  std::atomic<bool> feedback_recording_{false};
  std::atomic<uint64_t> feedback_frame_counter_{0};
  std::vector<std::string> feedback_frames_;

  // 写盘
  std::unique_ptr<FrameBuffer> buffer_;
  std::thread writer_thread_;
  std::ofstream pose_file_;        // JSONL 临时写入（stop 时合并为 JSON）
  std::ofstream metadata_file_;    // metadata.jsonl
  std::mutex image_mtx_;           // 保护图片写入（多相机回调并发）
  std::vector<std::string> pose_frames_;  // 缓存所有帧的 JSON 字符串（保持插入顺序）
  double session_start_time_;
  double session_end_time_;

  // GStreamer 录制器
  std::unique_ptr<GstRecorder> gst_recorder_;
};

}  // namespace dexe_recorder
