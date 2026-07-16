// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#pragma once

#include <gst/app/gstappsrc.h>
#include <gst/gst.h>

#include <string>
#include <vector>

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
 */
class GstRecorder
{
public:
    /** @brief 录制格式 */
    enum class Format
    {
        VIDEO,  ///< H.264 MP4（nvv4l2h264enc 硬编）
        JPEG    ///< 逐帧 JPEG（nvjpegenc 硬编）
    };

    /** @brief 构造函数 */
    GstRecorder();

    /** @brief 析构函数，自动停止录制 */
    ~GstRecorder();

    /**
     * @brief 初始化录制 pipeline
     * @param session_dir 录制目录（如 data/20260707_120000）
     * @param format VIDEO（H.264 MP4）或 JPEG（逐帧）
     * @return true 成功；false 失败
     */
    bool Start(const std::string& session_dir, Format format);

    /**
     * @brief 推入一帧 kfc_compressed 数据（JPEG bytes，左右目并排 3840x1080）
     * @param data JPEG 字节
     * @param size 字节数
     */
    void PushCompressedFrame(const uint8_t* data, size_t size);

    /**
     * @brief 推入一帧手部相机 Image 数据（BGR/RGB raw）
     * @param camera_name "hand_left" 或 "hand_right"
     * @param data 图像字节
     * @param size 字节数
     * @param width 图像宽度
     * @param height 图像高度
     * @param encoding 编码格式（如 "rgb8"、"bgr8"）
     */
    void PushHandFrame(const std::string& camera_name,
                       const uint8_t* data,
                       size_t size,
                       int width,
                       int height,
                       const std::string& encoding);

    /** @brief 停止录制，向 appsrc 发 EOS 并 flush pipeline */
    void Stop();

private:
    /**
     * @brief 创建头部相机 pipeline（含 JPEG 解码 + 左右目拆分）
     * @param session_dir 录制目录
     * @param format 录制格式
     * @return true 成功
     */
    bool CreateHeadPipeline(const std::string& session_dir, Format format);

    /**
     * @brief 创建手部相机 pipeline
     * @param session_dir 录制目录
     * @param camera_name 相机名（"hand_left" / "hand_right"）
     * @param format 录制格式
     * @return true 成功
     */
    bool CreateHandPipeline(const std::string& session_dir, const std::string& camera_name, Format format);

    /** @brief 销毁所有 pipeline */
    void DestroyAll();

    GstElement* head_pipeline_ = nullptr;  ///< 头部相机 pipeline
    GstAppSrc* head_appsrc_ = nullptr;     ///< 头部相机 appsrc

    GstElement* hand_left_pipeline_ = nullptr;  ///< 手部左相机 pipeline
    GstAppSrc* hand_left_appsrc_ = nullptr;     ///< 手部左相机 appsrc

    GstElement* hand_right_pipeline_ = nullptr;  ///< 手部右相机 pipeline
    GstAppSrc* hand_right_appsrc_ = nullptr;     ///< 手部右相机 appsrc

    Format format_ = Format::VIDEO;  ///< 当前录制格式
    bool started_ = false;           ///< 是否已启动
};

}  // namespace dexe_recorder
