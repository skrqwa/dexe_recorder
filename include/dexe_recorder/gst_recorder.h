// ----------------------------------------------------------------------------
// Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#pragma once

#include <gst/gst.h>
#include <gst/app/gstappsrc.h>
#include <string>
#include <vector>

namespace dexe_recorder {

// GStreamer 录制器：处理 kfc_compressed（左右目并排 JPEG）拆分 + 视频/图片保存
class GstRecorder {
 public:
  enum class Format { VIDEO, JPEG };

  GstRecorder();
  ~GstRecorder();

  // 初始化录制 pipeline
  // session_dir: 录制目录（如 data/recorded_auto/20260707_120000）
  // format: VIDEO（H.264 MP4）或 JPEG（逐帧）
  // 返回 true 成功
  bool Start(const std::string& session_dir, Format format);

  // 推入一帧 kfc_compressed 数据（JPEG bytes，左右目并排）
  void PushCompressedFrame(const uint8_t* data, size_t size);

  // 推入一帧手部相机 Image 数据（BGR/RGB raw）
  // camera_name: "hand_left" / "hand_right"
  void PushHandFrame(const std::string& camera_name, const uint8_t* data, size_t size,
                     int width, int height, const std::string& encoding);

  // 停止录制，flush pipeline
  void Stop();

 private:
  bool CreateHeadPipeline(const std::string& session_dir, Format format);
  bool CreateHandPipeline(const std::string& session_dir, const std::string& camera_name,
                          Format format);
  void DestroyAll();

  GstElement* head_pipeline_ = nullptr;
  GstAppSrc* head_appsrc_ = nullptr;

  GstElement* hand_left_pipeline_ = nullptr;
  GstAppSrc* hand_left_appsrc_ = nullptr;

  GstElement* hand_right_pipeline_ = nullptr;
  GstAppSrc* hand_right_appsrc_ = nullptr;

  Format format_ = Format::VIDEO;
  bool started_ = false;
};

}  // namespace dexe_recorder
