// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#include "dexe_recorder/media_timeline.h"

namespace dexe_recorder
{

namespace
{

// 视频时间轴的纳秒单位。
constexpr uint64_t kNanosecondsPerSecond = 1000000000ULL;

// 录制视频的固定帧率。
constexpr uint64_t kVideoFrameRate = 30ULL;

}  // namespace

/**
 * @brief 按帧序号生成 30 fps 媒体时间并检测源时间戳异常
 * @param source_timestamp_ns 当前帧的原始 ROS 时间戳（Unix 纳秒）
 * @return 下一帧的 PTS、持续时间与源时间戳状态
 * @throws 不抛出异常
 */
MediaFrameTiming MediaTimeline::Next(uint64_t source_timestamp_ns) noexcept
{
    const uint64_t previous_source_timestamp_ns = last_source_timestamp_ns_;
    SourceTimestampStatus status = SourceTimestampStatus::NORMAL;
    if (has_frame_ && source_timestamp_ns == last_source_timestamp_ns_)
    {
        status = SourceTimestampStatus::DUPLICATE;
    }
    else if (has_frame_ && source_timestamp_ns < last_source_timestamp_ns_)
    {
        status = SourceTimestampStatus::REGRESSED;
    }

    const uint64_t pts_ns = frame_index_ * kNanosecondsPerSecond / kVideoFrameRate;
    const uint64_t next_pts_ns = (frame_index_ + 1ULL) * kNanosecondsPerSecond / kVideoFrameRate;

    last_source_timestamp_ns_ = source_timestamp_ns;
    ++frame_index_;
    has_frame_ = true;
    return MediaFrameTiming{pts_ns, next_pts_ns - pts_ns, status, previous_source_timestamp_ns};
}

}  // namespace dexe_recorder
