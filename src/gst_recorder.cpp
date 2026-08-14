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

#include <algorithm>
#include <cstring>
#include <filesystem>
#include <vector>

namespace dexe_recorder
{

/**
 * @brief 构造函数
 *
 * 不在构造函数里调用 gst_init，因为 ROS2/rclcpp 可能已初始化 GStreamer，
 * 重复初始化会导致警告。gst_init 在 Start() 中延迟调用。
 * @return 构造完成的录制器
 * @throws 不抛出异常
 */
GstRecorder::GstRecorder()
{
    // 不在构造函数里调 gst_init（ROS2/rclcpp 可能已初始化 GStreamer）
}

/**
 * @brief 析构函数
 *
 * 自动销毁所有 pipeline，释放 GStreamer 资源。
 * @return 无
 * @throws 不抛出异常
 */
GstRecorder::~GstRecorder()
{
    DestroyAll();
}

/**
 * @brief 初始化录制 pipeline
 *
 * 延迟初始化 GStreamer（首次调用时 gst_init）并保存 session 上下文。
 * 各相机 pipeline 只在该相机首帧到达时创建。
 *
 * @param session_dir 录制目录（如 data/20260707_120000）
 * @param format VIDEO（H.264 MP4）或 JPEG（逐帧）
 * @return true 初始化成功；false 当前已启动
 * @throws std::bad_alloc 内部字符串状态分配失败
 */
bool GstRecorder::Start(const std::string& session_dir, Format format)
{
    if (started_)
    {
        g_printerr("[RECORDER_ALREADY_STARTED] session=%s\n", session_dir_.c_str());
        return false;
    }

    static bool gst_inited = false;
    if (!gst_inited)
    {
        gst_init(nullptr, nullptr);
        gst_inited = true;
    }

    session_dir_ = session_dir;
    format_ = format;
    head_timing_ = StreamTiming{};
    hand_left_timing_ = StreamTiming{};
    hand_right_timing_ = StreamTiming{};
    hand_left_width_ = 0;
    hand_left_height_ = 0;
    hand_left_encoding_.clear();
    hand_right_width_ = 0;
    hand_right_height_ = 0;
    hand_right_encoding_.clear();
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
 * @throws std::bad_alloc pipeline 字符串分配失败
 * @throws std::filesystem::filesystem_error 创建相机输出目录失败
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
        "appsrc name=head_src format=time do-timestamp=false is-live=true block=true max-bytes=8388608 "
        "caps=\"image/jpeg,width=3840,height=1080,framerate=30/1\" "
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
        gst_object_unref(head_appsrc_);
        head_appsrc_ = nullptr;
        gst_element_set_state(head_pipeline_, GST_STATE_NULL);
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
 * @param width 图像宽度
 * @param height 图像高度
 * @param encoding ROS 图像编码（"rgb8" 或 "bgr8"）
 * @return true 创建成功；false 创建失败
 * @throws std::bad_alloc pipeline 字符串分配失败
 * @throws std::filesystem::filesystem_error 创建相机输出目录失败
 */
bool GstRecorder::CreateHandPipeline(const std::string& session_dir,
                                     const std::string& camera_name,
                                     Format format,
                                     int width,
                                     int height,
                                     const std::string& encoding)
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

    const std::string gst_format = (encoding == "rgb8") ? "RGB" : "BGR";

    // 手部相机是 raw Image，用 appsrc 推 raw video
    // video 模式需 nvvidconv 转 NVMM 给 nvv4l2h264enc
    std::string pipe_str;
    if (format == Format::VIDEO)
    {
        pipe_str = "appsrc name=" + appsrc_name +
                   " format=time do-timestamp=false is-live=true block=true max-bytes=8388608 "
                   "caps=\"video/x-raw,format=" +
                   gst_format + ",width=" + std::to_string(width) + ",height=" + std::to_string(height) +
                   ",framerate=30/1\" "
                   "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 "
                   "! nvvidconv compute-hw=1 "
                   "! video/x-raw(memory:NVMM),format=I420 "
                   "! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 " +
                   encode_branch + sink;
    }
    else
    {
        pipe_str = "appsrc name=" + appsrc_name +
                   " format=time do-timestamp=false is-live=true block=true max-bytes=8388608 "
                   "caps=\"video/x-raw,format=" +
                   gst_format + ",width=" + std::to_string(width) + ",height=" + std::to_string(height) +
                   ",framerate=30/1\" "
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

    GstStateChangeReturn ret = gst_element_set_state(*pipeline_ptr, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_FAILURE || !CheckBus(*pipeline_ptr, camera_name))
    {
        g_printerr("[RECORDER_PIPELINE_START_FAILED] stream=%s\n", camera_name.c_str());
        gst_object_unref(*appsrc_ptr);
        *appsrc_ptr = nullptr;
        gst_element_set_state(*pipeline_ptr, GST_STATE_NULL);
        gst_object_unref(*pipeline_ptr);
        *pipeline_ptr = nullptr;
        return false;
    }
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
 * @param timestamp_ns ROS 源时间戳（Unix 纳秒）
 * @return true 帧已成功交给 pipeline；false 帧未录入
 * @throws std::bad_alloc pipeline 字符串或内部缓冲分配失败
 */
bool GstRecorder::PushCompressedFrame(const uint8_t* data, size_t size, uint64_t timestamp_ns)
{
    if (!started_ || data == nullptr || size == 0)
    {
        return false;
    }

    std::lock_guard<std::mutex> lock(head_mutex_);
    if (!head_pipeline_ && !CreateHeadPipeline(session_dir_, format_))
    {
        g_printerr("[RECORDER_PIPELINE_CREATE_FAILED] stream=head\n");
        return false;
    }
    return PushBuffer(head_appsrc_, head_pipeline_, data, size, timestamp_ns, &head_timing_, "head");
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
 * @param encoding 编码格式（"rgb8" 或 "bgr8"）
 * @param step 每行字节数，允许存在行尾 padding
 * @param timestamp_ns ROS 源时间戳（Unix 纳秒）
 * @return true 帧已成功交给 pipeline；false 帧未录入
 * @throws std::bad_alloc pipeline 字符串、行去 padding 缓冲或内部状态分配失败
 */
bool GstRecorder::PushHandFrame(const std::string& camera_name,
                                const uint8_t* data,
                                size_t size,
                                int width,
                                int height,
                                const std::string& encoding,
                                size_t step,
                                uint64_t timestamp_ns)
{
    if (!started_ || data == nullptr || width <= 0 || height <= 0)
    {
        return false;
    }
    if (camera_name != "hand_left" && camera_name != "hand_right")
    {
        g_printerr("[RECORDER_UNSUPPORTED_CAMERA] stream=%s\n", camera_name.c_str());
        return false;
    }
    if (encoding != "rgb8" && encoding != "bgr8")
    {
        g_printerr("[RECORDER_UNSUPPORTED_ENCODING] stream=%s encoding=%s\n", camera_name.c_str(), encoding.c_str());
        return false;
    }

    const size_t row_bytes = static_cast<size_t>(width) * 3U;
    if (step < row_bytes || size < step * static_cast<size_t>(height))
    {
        g_printerr("[RECORDER_INVALID_IMAGE_LAYOUT] stream=%s size=%zu width=%d height=%d step=%zu\n",
                   camera_name.c_str(),
                   size,
                   width,
                   height,
                   step);
        return false;
    }

    std::mutex* stream_mutex = nullptr;
    GstElement** pipeline = nullptr;
    GstAppSrc** appsrc = nullptr;
    StreamTiming* timing = nullptr;
    int* configured_width = nullptr;
    int* configured_height = nullptr;
    std::string* configured_encoding = nullptr;
    if (camera_name == "hand_left")
    {
        stream_mutex = &hand_left_mutex_;
        pipeline = &hand_left_pipeline_;
        appsrc = &hand_left_appsrc_;
        timing = &hand_left_timing_;
        configured_width = &hand_left_width_;
        configured_height = &hand_left_height_;
        configured_encoding = &hand_left_encoding_;
    }
    else
    {
        stream_mutex = &hand_right_mutex_;
        pipeline = &hand_right_pipeline_;
        appsrc = &hand_right_appsrc_;
        timing = &hand_right_timing_;
        configured_width = &hand_right_width_;
        configured_height = &hand_right_height_;
        configured_encoding = &hand_right_encoding_;
    }

    std::lock_guard<std::mutex> lock(*stream_mutex);
    if (!*pipeline)
    {
        if (!CreateHandPipeline(session_dir_, camera_name, format_, width, height, encoding))
        {
            g_printerr("[RECORDER_PIPELINE_CREATE_FAILED] stream=%s\n", camera_name.c_str());
            return false;
        }
        *configured_width = width;
        *configured_height = height;
        *configured_encoding = encoding;
    }
    else if (*configured_width != width || *configured_height != height || *configured_encoding != encoding)
    {
        g_printerr("[RECORDER_IMAGE_FORMAT_CHANGED] stream=%s expected=%dx%d/%s actual=%dx%d/%s\n",
                   camera_name.c_str(),
                   *configured_width,
                   *configured_height,
                   configured_encoding->c_str(),
                   width,
                   height,
                   encoding.c_str());
        return false;
    }

    if (step == row_bytes)
    {
        return PushBuffer(
            *appsrc, *pipeline, data, row_bytes * static_cast<size_t>(height), timestamp_ns, timing, camera_name);
    }

    std::vector<uint8_t> packed(row_bytes * static_cast<size_t>(height));
    for (int row = 0; row < height; ++row)
    {
        std::memcpy(
            packed.data() + static_cast<size_t>(row) * row_bytes, data + static_cast<size_t>(row) * step, row_bytes);
    }
    return PushBuffer(*appsrc, *pipeline, packed.data(), packed.size(), timestamp_ns, timing, camera_name);
}

/**
 * @brief 推送带显式时间戳的 GStreamer buffer
 * @param appsrc 目标 appsrc
 * @param pipeline 对应 pipeline
 * @param data 紧密排列的帧数据
 * @param size 帧字节数
 * @param timestamp_ns ROS 源时间戳
 * @param timing 单路时间映射状态
 * @param stream_name 稳定日志流名称
 * @return true 推送成功且 bus 未报告错误；false 失败
 * @throws 不抛出异常
 */
bool GstRecorder::PushBuffer(GstAppSrc* appsrc,
                             GstElement* pipeline,
                             const uint8_t* data,
                             size_t size,
                             uint64_t timestamp_ns,
                             StreamTiming* timing,
                             const std::string& stream_name)
{
    if (!appsrc || !pipeline || !data || size == 0 || !timing || !CheckBus(pipeline, stream_name))
    {
        return false;
    }

    if (timing->first_timestamp_ns == 0)
    {
        timing->first_timestamp_ns = timestamp_ns;
    }
    GstClockTime pts = timestamp_ns >= timing->first_timestamp_ns
                           ? static_cast<GstClockTime>(timestamp_ns - timing->first_timestamp_ns)
                           : 0;
    if (timing->last_pts != GST_CLOCK_TIME_NONE && pts <= timing->last_pts)
    {
        const GstClockTime original_pts = pts;
        pts = timing->last_pts + 1;
        g_printerr("[RECORDER_NON_MONOTONIC_TIMESTAMP] stream=%s source_ns=%lu original_pts=%lu adjusted_pts=%lu\n",
                   stream_name.c_str(),
                   timestamp_ns,
                   original_pts,
                   pts);
    }

    GstBuffer* buffer = gst_buffer_new_allocate(nullptr, size, nullptr);
    if (!buffer)
    {
        g_printerr("[RECORDER_BUFFER_ALLOC_FAILED] stream=%s bytes=%zu\n", stream_name.c_str(), size);
        return false;
    }
    gst_buffer_fill(buffer, 0, data, size);
    GST_BUFFER_PTS(buffer) = pts;
    GST_BUFFER_DTS(buffer) = pts;
    GST_BUFFER_DURATION(buffer) = GST_SECOND / 30;

    const GstFlowReturn ret = gst_app_src_push_buffer(appsrc, buffer);
    if (ret != GST_FLOW_OK)
    {
        g_printerr("[RECORDER_PUSH_FAILED] stream=%s flow=%s(%d)\n",
                   stream_name.c_str(),
                   gst_flow_get_name(ret),
                   static_cast<int>(ret));
        CheckBus(pipeline, stream_name);
        return false;
    }
    timing->last_pts = pts;
    return CheckBus(pipeline, stream_name);
}

/**
 * @brief 非阻塞清空 pipeline bus，并报告其中的错误和警告
 * @param pipeline 要检查的 pipeline
 * @param stream_name 稳定日志流名称
 * @return true 未发现错误；false 发现错误
 * @throws 不抛出异常
 */
bool GstRecorder::CheckBus(GstElement* pipeline, const std::string& stream_name)
{
    if (!pipeline)
    {
        return false;
    }
    bool ok = true;
    GstBus* bus = gst_element_get_bus(pipeline);
    GstMessage* message = nullptr;
    while ((message = gst_bus_pop(bus)) != nullptr)
    {
        GError* error = nullptr;
        gchar* debug = nullptr;
        if (GST_MESSAGE_TYPE(message) == GST_MESSAGE_ERROR)
        {
            gst_message_parse_error(message, &error, &debug);
            g_printerr("[RECORDER_GST_ERROR] stream=%s message=%s debug=%s\n",
                       stream_name.c_str(),
                       error ? error->message : "unknown",
                       debug ? debug : "");
            ok = false;
        }
        else if (GST_MESSAGE_TYPE(message) == GST_MESSAGE_WARNING)
        {
            gst_message_parse_warning(message, &error, &debug);
            g_printerr("[RECORDER_GST_WARNING] stream=%s message=%s debug=%s\n",
                       stream_name.c_str(),
                       error ? error->message : "unknown",
                       debug ? debug : "");
        }
        if (error) g_error_free(error);
        if (debug) g_free(debug);
        gst_message_unref(message);
    }
    gst_object_unref(bus);
    return ok;
}

/**
 * @brief 停止录制，flush pipeline
 *
 * 向各 appsrc 发送 EOS（End Of Stream），等待 pipeline 处理完剩余帧
 * 并写出文件（qtmux 需要写 MP4 moov atom），然后销毁所有 pipeline。
 * 超时等待 10 秒。
 * @return true 所有已录制流均完成 EOS 封装；false 至少一路失败
 * @throws std::bad_alloc pipeline 日志字符串分配失败
 */
bool GstRecorder::Stop()
{
    if (!started_) return true;
    started_ = false;
    bool success = true;

    {
        std::lock_guard<std::mutex> lock(head_mutex_);
        if (head_pipeline_)
        {
            success = FinishPipeline(head_pipeline_, head_appsrc_, "head") && success;
        }
    }
    {
        std::lock_guard<std::mutex> lock(hand_left_mutex_);
        if (hand_left_pipeline_)
        {
            success = FinishPipeline(hand_left_pipeline_, hand_left_appsrc_, "hand_left") && success;
        }
    }
    {
        std::lock_guard<std::mutex> lock(hand_right_mutex_);
        if (hand_right_pipeline_)
        {
            success = FinishPipeline(hand_right_pipeline_, hand_right_appsrc_, "hand_right") && success;
        }
    }

    DestroyAll();
    return success;
}

/**
 * @brief 向单路 appsrc 发送 EOS 并等待完成或错误
 * @param pipeline 目标 pipeline
 * @param appsrc 目标 appsrc
 * @param stream_name 稳定日志流名称
 * @return true 收到 EOS；false pipeline 未创建、发生错误或超时
 * @throws 不抛出异常
 */
bool GstRecorder::FinishPipeline(GstElement* pipeline, GstAppSrc* appsrc, const std::string& stream_name)
{
    if (!pipeline || !appsrc)
    {
        return false;
    }
    const GstFlowReturn eos_ret = gst_app_src_end_of_stream(appsrc);
    if (eos_ret != GST_FLOW_OK)
    {
        g_printerr("[RECORDER_EOS_SEND_FAILED] stream=%s flow=%s(%d)\n",
                   stream_name.c_str(),
                   gst_flow_get_name(eos_ret),
                   static_cast<int>(eos_ret));
        CheckBus(pipeline, stream_name);
        return false;
    }

    GstBus* bus = gst_element_get_bus(pipeline);
    GstMessage* message = gst_bus_timed_pop_filtered(
        bus, 10 * GST_SECOND, static_cast<GstMessageType>(GST_MESSAGE_EOS | GST_MESSAGE_ERROR));
    bool ok = false;
    if (message && GST_MESSAGE_TYPE(message) == GST_MESSAGE_EOS)
    {
        ok = true;
    }
    else if (message && GST_MESSAGE_TYPE(message) == GST_MESSAGE_ERROR)
    {
        GError* error = nullptr;
        gchar* debug = nullptr;
        gst_message_parse_error(message, &error, &debug);
        g_printerr("[RECORDER_GST_ERROR] stream=%s message=%s debug=%s\n",
                   stream_name.c_str(),
                   error ? error->message : "unknown",
                   debug ? debug : "");
        if (error) g_error_free(error);
        if (debug) g_free(debug);
    }
    else
    {
        g_printerr("[RECORDER_EOS_TIMEOUT] stream=%s timeout_sec=10\n", stream_name.c_str());
    }
    if (message) gst_message_unref(message);
    gst_object_unref(bus);
    return ok;
}

/**
 * @brief 销毁所有 pipeline
 *
 * 将每条 pipeline 置为 NULL 状态（停止），释放 GStreamer 对象引用。
 * 依次释放 3 组 pipeline 和 appsrc。
 * @return 无
 * @throws 不抛出异常
 */
void GstRecorder::DestroyAll()
{
    if (head_pipeline_)
    {
        gst_element_set_state(head_pipeline_, GST_STATE_NULL);
        gst_object_unref(head_pipeline_);
        head_pipeline_ = nullptr;
    }
    if (head_appsrc_)
    {
        gst_object_unref(head_appsrc_);
        head_appsrc_ = nullptr;
    }
    if (hand_left_pipeline_)
    {
        gst_element_set_state(hand_left_pipeline_, GST_STATE_NULL);
        gst_object_unref(hand_left_pipeline_);
        hand_left_pipeline_ = nullptr;
    }
    if (hand_left_appsrc_)
    {
        gst_object_unref(hand_left_appsrc_);
        hand_left_appsrc_ = nullptr;
    }
    if (hand_right_pipeline_)
    {
        gst_element_set_state(hand_right_pipeline_, GST_STATE_NULL);
        gst_object_unref(hand_right_pipeline_);
        hand_right_pipeline_ = nullptr;
    }
    if (hand_right_appsrc_)
    {
        gst_object_unref(hand_right_appsrc_);
        hand_right_appsrc_ = nullptr;
    }
}

}  // namespace dexe_recorder
