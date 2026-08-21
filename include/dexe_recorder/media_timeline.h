// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#pragma once

#include <cstdint>

namespace dexe_recorder
{

/**
 * @brief 源时间戳与前一帧的顺序关系
 * @return 源时间戳状态枚举类型
 * @throws 不抛出异常
 */
enum class SourceTimestampStatus
{
    NORMAL,     ///< 首帧或源时间戳严格递增
    DUPLICATE,  ///< 源时间戳与前一帧相同
    REGRESSED   ///< 源时间戳小于前一帧
};

/**
 * @brief 一帧视频的独立媒体时间信息
 * @return 帧时间信息类型
 * @throws 不抛出异常
 */
struct MediaFrameTiming
{
    // 帧在视频时间轴上的展示时间。
    uint64_t pts_ns = 0;
    // 帧在视频时间轴上的持续时间。
    uint64_t duration_ns = 0;
    // 源时间戳状态。
    SourceTimestampStatus source_status = SourceTimestampStatus::NORMAL;
    // 前一帧原始 ROS 时间戳。
    uint64_t previous_source_timestamp_ns = 0;
};

/**
 * @brief 为单路视频生成 30 fps 媒体时间轴并检测源时间戳异常
 * @return 单路视频时间轴生成器类型
 * @throws 不抛出异常
 */
class MediaTimeline
{
public:
    /**
     * @brief 根据当前帧的源时间戳生成下一帧媒体时间
     * @param source_timestamp_ns 当前帧的原始 ROS 时间戳（Unix 纳秒）
     * @return 下一帧的 PTS、持续时间与源时间戳状态
     * @throws 不抛出异常
     */
    MediaFrameTiming Next(uint64_t source_timestamp_ns) noexcept;

private:
    uint64_t frame_index_ = 0;               ///< 下一帧的 30 fps 媒体帧序号
    uint64_t last_source_timestamp_ns_ = 0;  ///< 最近一帧的源时间戳
    bool has_frame_ = false;                 ///< 是否已生成至少一帧
};

}  // namespace dexe_recorder
