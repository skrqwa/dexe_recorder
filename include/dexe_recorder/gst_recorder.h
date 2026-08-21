// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#pragma once

#include <gst/app/gstappsrc.h>
#include <gst/gst.h>

#include <cstdint>
#include <mutex>
#include <string>

#include "dexe_recorder/media_timeline.h"

namespace dexe_recorder
{

/**
 * @brief GStreamer 录制器：处理 kfc_compressed（左右目并排 JPEG）拆分 +
 * 视频/图片保存
 *
 * 管理 3 条独立 GStreamer pipeline：
 * - Head pipeline：头部相机 JPEG 拆分为左右目
 * - Hand left pipeline：手部左相机
 * - Hand right pipeline：手部右相机
 * @return GStreamer 录制器类型
 * @throws 不抛出异常
 */
class GstRecorder
{
public:
    /**
     * @brief 录制格式
     * @return 录制格式枚举类型
     * @throws 不抛出异常
     */
    enum class Format
    {
        VIDEO,  ///< H.264 MP4（nvv4l2h264enc 硬编）
        JPEG    ///< 逐帧 JPEG（nvjpegenc 硬编）
    };

    /**
     * @brief 构造空闲的 GStreamer 录制器
     * @return 构造完成的录制器
     * @throws 不抛出异常
     */
    GstRecorder();

    /**
     * @brief 析构函数，销毁仍存在的 pipeline
     * @return 无
     * @throws 不抛出异常
     */
    ~GstRecorder();

    /**
     * @brief 初始化录制 pipeline
     * @param session_dir 录制目录（如 data/20260707_120000）
     * @param format VIDEO（H.264 MP4）或 JPEG（逐帧）
     * @return true 成功；false 失败
     * @throws std::bad_alloc 内部字符串状态分配失败
     */
    bool Start(const std::string& session_dir, Format format);

    /**
     * @brief 推入一帧 kfc_compressed 数据（JPEG bytes，左右目并排 3840x1080）
     * @param data JPEG 字节
     * @param size 字节数
     * @param timestamp_ns ROS 源时间戳（Unix 纳秒）
     * @return true 帧已成功交给 pipeline；false 帧未录入
     * @throws std::bad_alloc pipeline 字符串或内部缓冲分配失败
     */
    bool PushCompressedFrame(const uint8_t* data, size_t size, uint64_t timestamp_ns);

    /**
     * @brief 推入一帧手部相机 Image 数据（BGR/RGB raw）
     * @param camera_name "hand_left" 或 "hand_right"
     * @param data 图像字节
     * @param size 字节数
     * @param width 图像宽度
     * @param height 图像高度
     * @param encoding 编码格式（如 "rgb8"、"bgr8"）
     * @param step 每行字节数，允许存在行尾 padding
     * @param timestamp_ns ROS 源时间戳（Unix 纳秒）
     * @return true 帧已成功交给 pipeline；false 帧未录入
     * @throws std::bad_alloc pipeline 字符串、行去 padding 缓冲或内部状态分配失败
     */
    bool PushHandFrame(const std::string& camera_name,
                       const uint8_t* data,
                       size_t size,
                       int width,
                       int height,
                       const std::string& encoding,
                       size_t step,
                       uint64_t timestamp_ns);

    /**
     * @brief 停止录制，向已创建的 appsrc 发 EOS 并 flush pipeline
     * @return true 所有已录制流均完成 EOS 封装；false 至少一路失败
     * @throws std::bad_alloc pipeline 字符串分配失败
     */
    bool Stop();

private:
    /**
     * @brief 创建头部相机 pipeline（含 JPEG 解码 + 左右目拆分）
     * @param session_dir 录制目录
     * @param format 录制格式
     * @return true 成功
     * @throws std::bad_alloc pipeline 字符串分配失败
     * @throws std::filesystem::filesystem_error 创建相机输出目录失败
     */
    bool CreateHeadPipeline(const std::string& session_dir, Format format);

    /**
     * @brief 创建手部相机 pipeline
     * @param session_dir 录制目录
     * @param camera_name 相机名（"hand_left" / "hand_right"）
     * @param format 录制格式
     * @param width 图像宽度
     * @param height 图像高度
     * @param encoding ROS 图像编码（"rgb8" 或 "bgr8"）
     * @return true 成功
     * @throws std::bad_alloc pipeline 字符串分配失败
     * @throws std::filesystem::filesystem_error 创建相机输出目录失败
     */
    bool CreateHandPipeline(const std::string& session_dir,
                            const std::string& camera_name,
                            Format format,
                            int width,
                            int height,
                            const std::string& encoding);

    /**
     * @brief 推送带显式时间戳的 GStreamer buffer
     * @param appsrc 目标 appsrc
     * @param pipeline 对应 pipeline
     * @param data 紧密排列的帧数据
     * @param size 帧字节数
     * @param timestamp_ns ROS 源时间戳
     * @param timeline 单路独立媒体时间轴
     * @param stream_name 稳定日志流名称
     * @return true 推送成功且 bus 未报告错误；false 失败
     * @throws 不抛出异常
     */
    bool PushBuffer(GstAppSrc* appsrc,
                    GstElement* pipeline,
                    const uint8_t* data,
                    size_t size,
                    uint64_t timestamp_ns,
                    MediaTimeline* timeline,
                    const std::string& stream_name);

    /**
     * @brief 非阻塞清空 pipeline bus，并报告其中的错误和警告
     * @param pipeline 要检查的 pipeline
     * @param stream_name 稳定日志流名称
     * @return true 未发现错误；false 发现错误
     * @throws 不抛出异常
     */
    bool CheckBus(GstElement* pipeline, const std::string& stream_name);

    /**
     * @brief 向单路 appsrc 发送 EOS 并等待完成或错误
     * @param pipeline 目标 pipeline
     * @param appsrc 目标 appsrc
     * @param stream_name 稳定日志流名称
     * @return true 收到 EOS；false 发生错误或超时
     * @throws 不抛出异常
     */
    bool FinishPipeline(GstElement* pipeline, GstAppSrc* appsrc, const std::string& stream_name);

    /**
     * @brief 销毁所有 pipeline
     * @return 无
     * @throws 不抛出异常
     */
    void DestroyAll();

    GstElement* head_pipeline_ = nullptr;  ///< 头部相机 pipeline
    GstAppSrc* head_appsrc_ = nullptr;     ///< 头部相机 appsrc

    GstElement* hand_left_pipeline_ = nullptr;  ///< 手部左相机 pipeline
    GstAppSrc* hand_left_appsrc_ = nullptr;     ///< 手部左相机 appsrc

    GstElement* hand_right_pipeline_ = nullptr;  ///< 手部右相机 pipeline
    GstAppSrc* hand_right_appsrc_ = nullptr;     ///< 手部右相机 appsrc

    std::string session_dir_;        ///< 当前录制 session 目录，供首帧懒创建 pipeline
    Format format_ = Format::VIDEO;  ///< 当前录制格式
    bool started_ = false;           ///< 是否已启动

    MediaTimeline head_timeline_;        ///< 头部左右目共享输入的媒体时间轴
    MediaTimeline hand_left_timeline_;   ///< 手部左相机媒体时间轴
    MediaTimeline hand_right_timeline_;  ///< 手部右相机媒体时间轴

    int hand_left_width_ = 0;          ///< 手部左相机首帧宽度
    int hand_left_height_ = 0;         ///< 手部左相机首帧高度
    std::string hand_left_encoding_;   ///< 手部左相机首帧编码
    int hand_right_width_ = 0;         ///< 手部右相机首帧宽度
    int hand_right_height_ = 0;        ///< 手部右相机首帧高度
    std::string hand_right_encoding_;  ///< 手部右相机首帧编码

    std::mutex head_mutex_;        ///< 保护头部 pipeline 创建与推送
    std::mutex hand_left_mutex_;   ///< 保护手部左 pipeline 创建与推送
    std::mutex hand_right_mutex_;  ///< 保护手部右 pipeline 创建与推送
};

}  // namespace dexe_recorder
