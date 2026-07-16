// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
/**
 * @file gst_recorder.cpp
 * @brief GStreamer 录制器实现：3 条 pipeline 管理与数据推送
 *
 * Head pipeline: appsrc -> jpegparse -> nvjpegdec(硬解) ->
 * nvvidconv(NVMM->sysmem)
 *                -> tee -> [videocrop 左/右] -> nvvidconv -> nvv4l2h264enc ->
 * qtmux -> filesink Hand pipeline:  appsrc -> nvvidconv -> nvv4l2h264enc ->
 * qtmux -> filesink
 */
#include "dexe_recorder/gst_recorder.h"

#include <cstring>
#include <filesystem>

namespace dexe_recorder
{

/**
 * @brief 构造函数
 *
 * 不在构造函数里调用 gst_init，因为 ROS2/rclcpp 可能已初始化 GStreamer，
 * 重复初始化会导致警告。gst_init 在 Start() 中延迟调用。
 */
GstRecorder::GstRecorder()
{
    // 不在构造函数里调 gst_init（ROS2/rclcpp 可能已初始化 GStreamer）
}

/**
 * @brief 析构函数
 *
 * 自动销毁所有 pipeline，释放 GStreamer 资源。
 */
GstRecorder::~GstRecorder()
{
    DestroyAll();
}

/**
 * @brief 初始化录制 pipeline
 *
 * 延迟初始化 GStreamer（首次调用时 gst_init），创建 3 条 pipeline：
 * - Head pipeline（头部相机 JPEG 拆分）
 * - Hand left pipeline（手部左相机）
 * - Hand right pipeline（手部右相机）
 *
 * @param session_dir 录制目录（如 data/20260707_120000）
 * @param format VIDEO（H.264 MP4）或 JPEG（逐帧）
 * @return true 成功；false pipeline 创建失败
 */
bool GstRecorder::Start(const std::string& session_dir, Format format)
{
    static bool gst_inited = false;
    if (!gst_inited)
    {
        gst_init(nullptr, nullptr);
        gst_inited = true;
    }

    format_ = format;

    if (!CreateHeadPipeline(session_dir, format))
    {
        return false;
    }
    if (!CreateHandPipeline(session_dir, "hand_left", format))
    {
        return false;
    }
    if (!CreateHandPipeline(session_dir, "hand_right", format))
    {
        return false;
    }

    started_ = true;
    return true;
}

/**
 * @brief 创建头部相机 pipeline
 *
 * kfc_compressed 是左右目并排 JPEG（如 3840x1080），需要用 videocrop 拆分：
 * appsrc -> jpegparse -> nvjpegdec(硬解) -> nvvidconv(NVMM->sysmem) -> tee
 *   ├-> videocrop(左 0-1920) -> nvvidconv -> nvv4l2h264enc -> qtmux ->
 * filesink(head/left/video.mp4) └-> videocrop(右 1920-3840) -> nvvidconv ->
 * nvv4l2h264enc -> qtmux -> filesink(head/right/video.mp4)
 *
 * @param session_dir 录制目录
 * @param format 录制格式（VIDEO 或 JPEG）
 * @return true 创建成功；false 创建失败
 */
bool GstRecorder::CreateHeadPipeline(const std::string& session_dir, Format format)
{
    // kfc_compressed 是左右目并排 JPEG（如 3840x1080）
    // pipeline: appsrc -> jpegparse -> nvjpegdec -> tee
    //   -> videocrop left=0 right=half_w  -> [video|jpeg encode] -> sink (left)
    //   -> videocrop left=half_w right=0  -> [video|jpeg encode] -> sink (right)

    std::string left_path, right_path;
    if (format == Format::VIDEO)
    {
        // 视频文件放在 head/left/ 和 head/right/ 目录下（与遥操目录结构一致）
        std::filesystem::create_directories(session_dir + "/head/left");
        std::filesystem::create_directories(session_dir + "/head/right");
        left_path = session_dir + "/head/left/video.mp4";
        right_path = session_dir + "/head/right/video.mp4";
    }
    else
    {
        left_path = session_dir + "/head/left/%06d.jpg";
        right_path = session_dir + "/head/right/%06d.jpg";
        std::filesystem::create_directories(session_dir + "/head/left");
        std::filesystem::create_directories(session_dir + "/head/right");
    }

    // kfc_compressed 实际分辨率 3840x1080（左右目并排，每目 1920x1080）
    // 与遥操 gst_jpeg_writer.py start_split_recording(3840, 1080) 一致
    const int input_width = 3840;
    const int input_height = 1080;
    const int half_w = input_width / 2;  // 1920

    std::string encode_branch;
    std::string sink_left, sink_right;
    if (format == Format::VIDEO)
    {
        // video 模式：videocrop 裁切 -> nvvidconv 转 NVMM -> nvv4l2h264enc 硬编 ->
        // mp4mux
        encode_branch =
            "! nvvidconv compute-hw=1 "
            "! video/x-raw(memory:NVMM),format=I420 "
            "! nvv4l2h264enc bitrate=4000000 control-rate=0 peak-bitrate=8000000 "
            "idrinterval=30 iframeinterval=30 insert-sps-pps=true "
            "profile=0 num-B-Frames=0 "
            "! h264parse ! qtmux ";
        sink_left = "! filesink location=\"" + left_path + "\"";
        sink_right = "! filesink location=\"" + right_path + "\"";
    }
    else
    {
        encode_branch = "! nvjpegenc quality=80 ";
        sink_left = "! multifilesink location=\"" + left_path + "\"";
        sink_right = "! multifilesink location=\"" + right_path + "\"";
    }

    // 参考遥操：nvjpegdec 输出 NVMM，需 nvvidconv 转系统内存再做软件裁切
    std::string pipe_str =
        "appsrc name=head_src format=time do-timestamp=true is-live=true "
        "caps=\"image/jpeg\" "
        "! jpegparse "
        "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 "
        "! nvjpegdec "
        "! nvvidconv "
        "! video/x-raw,format=I420 "
        "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 "
        "! tee name=t allow-not-linked=false "
        // 左目：videocrop 裁掉右半
        "t. ! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 "
        "! videocrop left=0 right=" +
        std::to_string(half_w) +
        " top=0 bottom=0 "
        "! video/x-raw,format=I420,width=" +
        std::to_string(half_w) + ",height=" + std::to_string(input_height) +
        " "
        "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 " +
        encode_branch + sink_left +
        " "
        // 右目：videocrop 裁掉左半
        "t. ! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 "
        "! videocrop left=" +
        std::to_string(half_w) +
        " right=0 top=0 bottom=0 "
        "! video/x-raw,format=I420,width=" +
        std::to_string(half_w) + ",height=" + std::to_string(input_height) +
        " "
        "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 " +
        encode_branch + sink_right;

    GError* err = nullptr;
    head_pipeline_ = gst_parse_launch(pipe_str.c_str(), &err);
    if (err)
    {
        g_printerr("Head pipeline parse warning/error: %s\n", err->message);
        g_error_free(err);
        err = nullptr;
    }
    if (!head_pipeline_)
    {
        g_printerr("Failed to create head pipeline\n");
        return false;
    }

    head_appsrc_ = GST_APP_SRC(gst_bin_get_by_name(GST_BIN(head_pipeline_), "head_src"));
    if (!head_appsrc_)
    {
        g_printerr("Failed to get head_src appsrc from pipeline\n");
        gst_object_unref(head_pipeline_);
        head_pipeline_ = nullptr;
        return false;
    }
    gst_app_src_set_stream_type(head_appsrc_, GST_APP_STREAM_TYPE_STREAM);

    GstStateChangeReturn ret = gst_element_set_state(head_pipeline_, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_FAILURE)
    {
        g_printerr("Failed to set head pipeline to PLAYING\n");
        GstBus* bus = gst_element_get_bus(head_pipeline_);
        GstMessage* msg = gst_bus_timed_pop(bus, 1 * GST_SECOND);
        if (msg && GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR)
        {
            gchar* debug;
            GError* gerr;
            gst_message_parse_error(msg, &gerr, &debug);
            g_printerr("Pipeline error: %s (%s)\n", gerr->message, debug);
            g_error_free(gerr);
            g_free(debug);
        }
        if (msg) gst_message_unref(msg);
        gst_object_unref(bus);
        gst_object_unref(head_pipeline_);
        head_pipeline_ = nullptr;
        return false;
    }
    return true;
}

/**
 * @brief 创建手部相机 pipeline
 *
 * 手部相机是单目 raw Image（如 640x360 BGR），无需拆分：
 * appsrc -> nvvidconv -> nvv4l2h264enc -> qtmux ->
 * filesink(hand/{camera_name}/video.mp4)
 *
 * @param session_dir 录制目录
 * @param camera_name 相机名（"hand_left" 或 "hand_right"）
 * @param format 录制格式（VIDEO 或 JPEG）
 * @return true 创建成功；false 创建失败
 */
bool GstRecorder::CreateHandPipeline(const std::string& session_dir, const std::string& camera_name, Format format)
{
    std::string path;
    std::string appsrc_name;
    GstElement** pipeline_ptr;
    GstAppSrc** appsrc_ptr;

    if (camera_name == "hand_left")
    {
        appsrc_name = "hand_left_src";
        pipeline_ptr = &hand_left_pipeline_;
        appsrc_ptr = &hand_left_appsrc_;
    }
    else
    {
        appsrc_name = "hand_right_src";
        pipeline_ptr = &hand_right_pipeline_;
        appsrc_ptr = &hand_right_appsrc_;
    }

    if (format == Format::VIDEO)
    {
        // 视频文件放在 hand/left/ 和 hand/right/ 目录下（与遥操目录结构一致）
        std::string dir = camera_name;  // hand_left 或 hand_right
        std::string sub = (camera_name == "hand_left") ? "hand/left" : "hand/right";
        std::filesystem::create_directories(session_dir + "/" + sub);
        path = session_dir + "/" + sub + "/video.mp4";
    }
    else
    {
        std::string sub = (camera_name == "hand_left") ? "hand/left" : "hand/right";
        std::filesystem::create_directories(session_dir + "/" + sub);
        path = session_dir + "/" + sub + "/%06d.jpg";
    }

    std::string encode_branch, sink;
    if (format == Format::VIDEO)
    {
        encode_branch =
            "! nvv4l2h264enc bitrate=4000000 control-rate=0 peak-bitrate=8000000 "
            "idrinterval=30 iframeinterval=30 insert-sps-pps=true "
            "profile=0 num-B-Frames=0 "
            "! h264parse ! qtmux ";
        sink = "! filesink location=\"" + path + "\"";
    }
    else
    {
        encode_branch = "! nvjpegenc quality=80 ";
        sink = "! multifilesink location=\"" + path + "\"";
    }

    // 手部相机是 raw Image，用 appsrc 推 raw video
    // video 模式需 nvvidconv 转 NVMM 给 nvv4l2h264enc
    std::string pipe_str;
    if (format == Format::VIDEO)
    {
        pipe_str = "appsrc name=" + appsrc_name +
                   " format=time do-timestamp=true is-live=true "
                   "caps=\"video/x-raw,format=BGR\" "
                   "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 "
                   "! nvvidconv compute-hw=1 "
                   "! video/x-raw(memory:NVMM),format=I420 "
                   "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 " +
                   encode_branch + sink;
    }
    else
    {
        pipe_str = "appsrc name=" + appsrc_name +
                   " format=time do-timestamp=true is-live=true "
                   "caps=\"video/x-raw,format=BGR\" "
                   "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 "
                   "! videoconvert " +
                   encode_branch + sink;
    }

    GError* err = nullptr;
    *pipeline_ptr = gst_parse_launch(pipe_str.c_str(), &err);
    if (err)
    {
        g_printerr("%s pipeline parse warning/error: %s\n", camera_name.c_str(), err->message);
        g_error_free(err);
        err = nullptr;
    }
    if (!*pipeline_ptr)
    {
        g_printerr("Failed to create %s pipeline\n", camera_name.c_str());
        return false;
    }

    *appsrc_ptr = GST_APP_SRC(gst_bin_get_by_name(GST_BIN(*pipeline_ptr), appsrc_name.c_str()));
    if (!*appsrc_ptr)
    {
        g_printerr("Failed to get %s appsrc from pipeline\n", camera_name.c_str());
        gst_object_unref(*pipeline_ptr);
        *pipeline_ptr = nullptr;
        return false;
    }
    gst_app_src_set_stream_type(*appsrc_ptr, GST_APP_STREAM_TYPE_STREAM);

    gst_element_set_state(*pipeline_ptr, GST_STATE_PLAYING);
    return true;
}

/**
 * @brief 推入一帧 kfc_compressed 数据
 *
 * 将 JPEG 字节推入头部 pipeline 的 appsrc，GStreamer 内部自动完成
 * 解码 -> 拆分左右目 -> 编码 -> 写盘。
 *
 * @param data JPEG 字节数据（3840x1080 左右目并排）
 * @param size 字节数
 */
void GstRecorder::PushCompressedFrame(const uint8_t* data, size_t size)
{
    if (!head_appsrc_ || !started_) return;

    GstBuffer* buf = gst_buffer_new_allocate(nullptr, size, nullptr);
    gst_buffer_fill(buf, 0, data, size);
    GstFlowReturn ret = gst_app_src_push_buffer(head_appsrc_, buf);
    if (ret != GST_FLOW_OK)
    {
        g_printerr("Failed to push head frame\n");
    }
}

/**
 * @brief 推入一帧手部相机 Image 数据
 *
 * 根据 camera_name 选择对应的 pipeline，将 raw Image 字节推入 appsrc。
 *
 * @param camera_name "hand_left" 或 "hand_right"
 * @param data 图像字节
 * @param size 字节数
 * @param width 图像宽度
 * @param height 图像高度
 * @param encoding 编码格式（如 "rgb8"、"bgr8"，当前未使用）
 */
void GstRecorder::PushHandFrame(const std::string& camera_name,
                                const uint8_t* data,
                                size_t size,
                                int width,
                                int height,
                                const std::string& /*encoding*/)
{
    GstAppSrc* src = nullptr;
    if (camera_name == "hand_left")
    {
        src = hand_left_appsrc_;
    }
    else
    {
        src = hand_right_appsrc_;
    }
    if (!src || !started_) return;

    GstBuffer* buf = gst_buffer_new_allocate(nullptr, size, nullptr);
    gst_buffer_fill(buf, 0, data, size);
    GstFlowReturn ret = gst_app_src_push_buffer(src, buf);
    if (ret != GST_FLOW_OK)
    {
        g_printerr("Failed to push %s frame\n", camera_name.c_str());
    }
}

/**
 * @brief 停止录制，flush pipeline
 *
 * 向各 appsrc 发送 EOS（End Of Stream），等待 pipeline 处理完剩余帧
 * 并写出文件（qtmux 需要写 MP4 moov atom），然后销毁所有 pipeline。
 * 超时等待 10 秒。
 */
void GstRecorder::Stop()
{
    if (!started_) return;
    started_ = false;

    // 发 EOS 到各 appsrc
    if (head_appsrc_)
    {
        gst_app_src_end_of_stream(head_appsrc_);
        if (head_pipeline_)
        {
            GstBus* bus = gst_element_get_bus(head_pipeline_);
            gst_bus_timed_pop_filtered(bus, 10 * GST_SECOND, GST_MESSAGE_EOS);
            gst_object_unref(bus);
        }
    }
    if (hand_left_appsrc_)
    {
        gst_app_src_end_of_stream(hand_left_appsrc_);
        if (hand_left_pipeline_)
        {
            GstBus* bus = gst_element_get_bus(hand_left_pipeline_);
            gst_bus_timed_pop_filtered(bus, 10 * GST_SECOND, GST_MESSAGE_EOS);
            gst_object_unref(bus);
        }
    }
    if (hand_right_appsrc_)
    {
        gst_app_src_end_of_stream(hand_right_appsrc_);
        if (hand_right_pipeline_)
        {
            GstBus* bus = gst_element_get_bus(hand_right_pipeline_);
            gst_bus_timed_pop_filtered(bus, 10 * GST_SECOND, GST_MESSAGE_EOS);
            gst_object_unref(bus);
        }
    }

    DestroyAll();
}

/**
 * @brief 销毁所有 pipeline
 *
 * 将每条 pipeline 置为 NULL 状态（停止），释放 GStreamer 对象引用。
 * 使用 lambda 统一处理 3 组 pipeline + appsrc。
 */
void GstRecorder::DestroyAll()
{
    auto destroy = [](GstElement** pipe, GstAppSrc** src)
    {
        if (*pipe)
        {
            gst_element_set_state(*pipe, GST_STATE_NULL);
            gst_object_unref(*pipe);
            *pipe = nullptr;
        }
        if (*src)
        {
            gst_object_unref(*src);
            *src = nullptr;
        }
    };
    destroy(&head_pipeline_, &head_appsrc_);
    destroy(&hand_left_pipeline_, &hand_left_appsrc_);
    destroy(&hand_right_pipeline_, &hand_right_appsrc_);
}

}  // namespace dexe_recorder
