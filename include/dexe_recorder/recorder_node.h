// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
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
#include <nlohmann/json.hpp>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "dexe_recorder/gst_recorder.h"
#include "end_effector_interfaces/msg/ee_joint_control.hpp"
#ifdef USE_EE_FEEDBACK
#include "end_effector_interfaces/msg/ee_feedback.hpp"
#endif
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/compressed_image.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "std_msgs/msg/string.hpp"
#include "std_srvs/srv/trigger.hpp"

namespace dexe_recorder
{

/**
 * @brief 单个数据帧（时间戳 + 各类关节数据 + 相机图像缓存）
 *
 * 从 /feedback/robot_server_state 的 JSON 解析而来，
 * 包含关节反馈和指令数据，供 30Hz 写线程取快照使用。
 */
struct Frame
{
    double timestamp;  ///< 时间戳（秒）

    // 关节数据（从 robot_server_state JSON 解析）
    std::vector<double> joint_position;      ///< 关节反馈位置
    std::vector<double> joint_position_cmd;  ///< 关节指令位置（命令）
    std::vector<double> joint_velocity;      ///< 关节反馈速度
    std::vector<double> joint_velocity_cmd;  ///< 关节指令速度
    std::vector<double> joint_torque;        ///< 关节反馈力矩
    std::vector<double> joint_torque_cmd;    ///< 关节指令力矩
    std::vector<std::string> joint_names;    ///< 关节名列表（与上述数组一一对应）

    // 末端执行器
    std::vector<double> ee_left_qpos;   ///< 左手末端执行器关节位置
    std::vector<double> ee_right_qpos;  ///< 右手末端执行器关节位置

    /**
     * @brief 相机图像数据（按相机名索引）
     */
    struct ImageData
    {
        std::string encoding;       ///< 编码格式："jpeg"/"png"/"rgb8"/"bgr8"
        int width;                  ///< 图像宽度（像素）
        int height;                 ///< 图像高度（像素）
        std::vector<uint8_t> data;  ///< 原始图像字节
    };

    std::unordered_map<std::string, ImageData> images;  ///< 相机名 -> 图像数据
};

/**
 * @brief 线程安全的有界帧缓冲队列（生产者-消费者模型）
 * @note 已弃用，改用 30Hz 写线程取最新快照方式，保留兼容
 */
class FrameBuffer
{
public:
    /**
     * @brief 构造函数
     * @param max_size 队列最大容量（默认 600 帧）
     */
    explicit FrameBuffer(size_t max_size = 600);

    /**
     * @brief 生产端：非阻塞推入帧
     * @param frame 要推入的帧（移动语义）
     * @note 队列满时丢弃最旧帧并计数
     */
    void Push(Frame&& frame);

    /**
     * @brief 消费端：阻塞等待并取出一帧
     * @param out 输出帧
     * @return true 取到帧；false 队列已停止
     */
    bool Pop(Frame* out);

    /** @brief 停止队列，唤醒所有等待的消费者 */
    void Stop();

    /**
     * @brief 获取丢弃帧数
     * @return 被丢弃的帧总数
     */
    size_t dropped_count() const
    {
        return dropped_.load();
    }

    /**
     * @brief 获取当前队列长度
     * @return 队列中帧的数量
     */
    size_t size() const;

private:
    mutable std::mutex mtx_;            ///< 互斥锁
    std::condition_variable cv_;        ///< 条件变量
    std::deque<Frame> queue_;           ///< 帧队列
    size_t max_size_;                   ///< 最大容量
    std::atomic<bool> stopped_{false};  ///< 停止标志
    std::atomic<size_t> dropped_{0};    ///< 丢弃计数
};

/**
 * @brief 录制配置（从 auto_recorder.yaml 加载）
 */
struct RecorderConfig
{
    // 数据源话题
    std::string state_topic = "/feedback/robot_server_state";         ///< 关节状态话题（~100Hz JSON）
    std::string head_compressed_topic = "/camera/kfc_compressed";     ///< 头部相机话题（~27Hz JPEG）
    std::string hand_left_topic = "/camera_l/color/image_rect_raw";   ///< 手部左相机话题（~30Hz）
    std::string hand_right_topic = "/camera_r/color/image_rect_raw";  ///< 手部右相机话题（~30Hz）
    std::string ee_left_topic = "/feedback/ee/left";                ///< EE 反馈左话题（~20Hz）
    std::string ee_right_topic = "/feedback/ee/right";              ///< EE 反馈右话题（~20Hz）

    // 存储路径
    std::string output_dir = "data/recorded_auto";  ///< 输出目录

    // 保存格式
    std::string save_format = "video";  ///< 保存格式："video"（H.264 MP4）或 "jpeg"（逐帧）

    // 录制参数
    int max_buffer_frames = 600;          ///< 帧缓冲大小（已弃用）
    double max_segment_duration_sec = 0;  ///< 最大录制时长（秒），0=不限时

    /**
     * @brief 从 YAML 文件加载配置
     * @param path YAML 文件路径
     * @return 加载后的配置，失败时返回默认值
     */
    static RecorderConfig Load(const std::string& path);
};

/**
 * @brief dexe_recorder 主节点，负责 auto 模式下的数据采集录制
 *
 * 订阅关节状态、相机图像、末端执行器等话题，生成：
 * - pose_record.json：命令数据（joint_position_cmd + EE 命令），30Hz
 * - feedback_record.json：反馈数据（joint_position + EE 位置），30Hz
 * - head/hand video.mp4：相机视频（GStreamer 硬件编码）
 * - metadata.jsonl：相机帧元数据
 *
 * 架构：ROS 回调只更新内存快照，两个 30Hz 独立写线程取快照流式写盘。
 */
class RecorderNode : public rclcpp::Node
{
public:
    /**
     * @brief 构造函数
     * @param options ROS2 节点选项
     */
    explicit RecorderNode(const rclcpp::NodeOptions& options = rclcpp::NodeOptions());

    /** @brief 析构函数，停止录制并清理资源 */
    ~RecorderNode() override;

private:
    // ---- 服务回调 ----

    /** @brief 处理 start_recording 服务请求 */
    void HandleStart(const std::shared_ptr<std_srvs::srv::Trigger::Request> req,
                     std::shared_ptr<std_srvs::srv::Trigger::Response> res);

    /** @brief 处理 stop_recording 服务请求 */
    void HandleStop(const std::shared_ptr<std_srvs::srv::Trigger::Request> req,
                    std::shared_ptr<std_srvs::srv::Trigger::Response> res);

    /** @brief 处理 get_status 服务请求 */
    void HandleStatus(const std::shared_ptr<std_srvs::srv::Trigger::Request> req,
                      std::shared_ptr<std_srvs::srv::Trigger::Response> res);

    // ---- 话题回调（只更新内存快照，不碰磁盘）----

    /** @brief 关节状态回调（~100Hz），解析 JSON 并更新 latest_state_frame_ */
    void OnState(const std_msgs::msg::String::ConstSharedPtr msg);

    /**
     * @brief 手部相机回调（~30Hz），推入 GStreamer pipeline 并写 metadata
     * @param camera_name "hand_left" 或 "hand_right"
     */
    void OnImage(const std::string& camera_name, const sensor_msgs::msg::Image::ConstSharedPtr msg);

    /** @brief 头部相机回调（~27Hz），推入 GStreamer pipeline 并写 metadata */
    void OnCompressedImage(const sensor_msgs::msg::CompressedImage::ConstSharedPtr msg);

    /**
     * @brief EE 反馈回调（~20Hz），更新 latest_ee_values_（经关节名映射）
     * @param side "left" 或 "right"
     */
#ifdef USE_EE_FEEDBACK
    void OnEndEffector(const std::string& side, const end_effector_interfaces::msg::EEFeedback::ConstSharedPtr msg);
#endif

    /**
     * @brief EE 命令回调（事件驱动），更新 latest_ee_cmd_values_
     * @param side "left" 或 "right"
     */
    void OnEeCommand(const std::string& side, const end_effector_interfaces::msg::EEJointControl::ConstSharedPtr msg);

    // ---- 30Hz 写盘线程 ----

    /** @brief pose_record 写线程（30Hz），取最新命令快照写盘 */
    void WriterLoop();

    /** @brief feedback_record 写线程（30Hz），取最新反馈快照写盘 */
    void FeedbackWriterLoop();

    // ---- 录制会话管理 ----

    /**
     * @brief 开始录制：创建 session 目录、打开文件、启动写线程
     * @return true 成功；false 已在录制或初始化失败
     */
    bool StartRecording();

    /**
     * @brief 停止录制：停写线程、flush pipeline、合并 JSON
     * @return true 成功；false 未在录制
     */
    bool StopRecording();

    // ---- 辅助方法 ----

    /**
     * @brief 生成 session ID（时间戳格式 YYYYMMDD_HHMMSS）
     * @return session ID 字符串
     */
    std::string MakeSessionId() const;

    /**
     * @brief 解析 robot_server_state 的 JSON 字符串
     * @param json_str JSON 字符串
     * @param frame 输出帧（填充 joint 字段）
     * @return true 解析成功
     */
    bool ParseStateJson(const std::string& json_str, Frame* frame);

    /**
     * @brief 组装并写入一帧 pose_record（命令数据）
     * @param frame 最新关节状态快照（取 joint_position_cmd）
     */
    void WriteFrame(const Frame& frame);

    /** @brief 合并临时 JSONL 为最终 pose_record.json */
    void FinalizePoseRecord();

    /** @brief 合并临时 JSONL 为最终 feedback_record.json */
    void FinalizeFeedbackRecord();

    /**
     * @brief 生成相机图片路径
     * @param camera_type 相机类型（"head_left" 等）
     * @param frame_id 帧序号
     * @return 相对路径（如 "head/left/000123.jpg"）
     */
    std::string ImagePath(const std::string& camera_type, uint64_t frame_id) const;

    /**
     * @brief EE 关节名映射（与遥操 ee_hand_mapping.py 规则一致）
     * @param side "left" 或 "right"
     * @param ee_name 末端设备型号（如 "Linker_L6"、"BrainCo_Revo1_R"）
     * @param joint_name 原始关节名（如 "T_MCP"）
     * @return 映射后的关节名（如 "LEFT_T_MCP" 或 "LEFT_HAND_THUMB1"）
     */
    std::string MapEeJointName(const std::string& side,
                               const std::string& ee_name,
                               const std::string& joint_name) const;

    RecorderConfig config_;  ///< 录制配置

    // ---- ROS2 接口 ----
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr state_sub_;                        ///< 关节状态订阅
    rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr head_compressed_sub_;  ///< 头部相机订阅
    std::vector<rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr> image_subs_;        ///< 手部相机订阅列表
#ifdef USE_EE_FEEDBACK
    rclcpp::Subscription<end_effector_interfaces::msg::EEFeedback>::SharedPtr ee_left_sub_;   ///< EE 反馈左订阅
    rclcpp::Subscription<end_effector_interfaces::msg::EEFeedback>::SharedPtr ee_right_sub_;  ///< EE 反馈右订阅
#endif
    rclcpp::Subscription<end_effector_interfaces::msg::EEJointControl>::SharedPtr ee_cmd_left_sub_;   ///< EE 命令左订阅
    rclcpp::Subscription<end_effector_interfaces::msg::EEJointControl>::SharedPtr ee_cmd_right_sub_;  ///< EE 命令右订阅
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr start_srv_;                                    ///< 开始录制服务
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr stop_srv_;                                     ///< 停止录制服务
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr status_srv_;                                   ///< 查询状态服务

    // ---- 录制状态 ----
    std::atomic<bool> recording_{false};            ///< 是否正在录制
    std::string current_session_;                   ///< 当前 session ID
    std::string current_session_dir_;               ///< 当前 session 目录路径
    std::atomic<uint64_t> frame_counter_{0};        ///< pose_record 帧计数
    std::atomic<uint64_t> image_frame_counter_{0};  ///< 全局图片序号（metadata frame_id）
    std::atomic<uint64_t> head_left_counter_{0};    ///< 头部左目独立序号
    std::atomic<uint64_t> head_right_counter_{0};   ///< 头部右目独立序号
    std::atomic<uint64_t> hand_left_counter_{0};    ///< 手部左目独立序号
    std::atomic<uint64_t> hand_right_counter_{0};   ///< 手部右目独立序号

    // ---- 内存快照（latest_state_mtx_ 保护）----
    std::mutex latest_state_mtx_;  ///< 快照互斥锁（回调控写、写线程读）
    Frame latest_state_frame_;     ///< 最新关节状态快照

    // ---- EE 反馈缓存（供 feedback_record 用）----
    std::map<std::string, double> latest_ee_values_;  ///< EE 反馈关节名->位置（经 MapEeJointName 映射）
    std::string ee_left_name_;                        ///< 左手末端型号（从 EEFeedback.ee_name 获取）
    std::string ee_right_name_;                       ///< 右手末端型号

    // ---- EE 命令缓存（供 pose_record 用）----
    std::map<std::string, double> latest_ee_cmd_values_;  ///< EE 命令关节名->值

    // ---- 反馈录制（30Hz 独立写线程）----
    std::ofstream feedback_file_;                      ///< feedback_record 临时 JSONL 文件
    std::thread feedback_writer_thread_;               ///< 反馈写线程
    std::atomic<bool> feedback_recording_{false};      ///< 反馈录制标志
    std::atomic<uint64_t> feedback_frame_counter_{0};  ///< 反馈帧计数
    std::vector<std::string> feedback_frames_;         ///< 反馈帧 JSON 字符串缓存（stop 时合并用）

    // ---- pose 录制 + 写盘 ----
    std::unique_ptr<FrameBuffer> buffer_;   ///< 帧缓冲队列（已弃用）
    std::thread writer_thread_;             ///< pose_record 写线程
    std::ofstream pose_file_;               ///< pose_record 临时 JSONL 文件
    std::ofstream metadata_file_;           ///< metadata.jsonl 文件
    std::mutex image_mtx_;                  ///< 保护 pose_frames_/feedback_frames_/metadata_file_
    std::vector<std::string> pose_frames_;  ///< pose 帧 JSON 字符串缓存（stop 时合并用）
    double session_start_time_;             ///< session 开始时间
    double session_end_time_;               ///< session 结束时间

    // ---- GStreamer 录制器 ----
    std::unique_ptr<GstRecorder> gst_recorder_;  ///< GStreamer 录制器（相机视频）
};

}  // namespace dexe_recorder
