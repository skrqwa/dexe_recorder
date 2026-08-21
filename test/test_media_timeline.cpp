// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#include <gtest/gtest.h>

#include "dexe_recorder/media_timeline.h"

namespace dexe_recorder
{

/**
 * @brief 验证重复源时间戳不会产生重叠的视频 PTS
 * @return 无
 * @throws 不抛出异常
 */
TEST(MediaTimelineTest, DuplicateSourceTimestampsKeepThirtyFpsPts)
{
    MediaTimeline timeline;

    const auto first = timeline.Next(1000000000ULL);
    const auto second = timeline.Next(1000000000ULL);
    const auto third = timeline.Next(1000000000ULL);

    EXPECT_EQ(first.pts_ns, 0ULL);
    EXPECT_EQ(second.pts_ns, 33333333ULL);
    EXPECT_EQ(third.pts_ns, 66666666ULL);
    EXPECT_EQ(second.source_status, SourceTimestampStatus::DUPLICATE);
    EXPECT_EQ(third.source_status, SourceTimestampStatus::DUPLICATE);
}

/**
 * @brief 验证源时间戳回退时媒体时间轴仍保持 30 fps
 * @return 无
 * @throws 不抛出异常
 */
TEST(MediaTimelineTest, RegressedSourceTimestampKeepsThirtyFpsPts)
{
    MediaTimeline timeline;

    const auto first = timeline.Next(2000000000ULL);
    const auto second = timeline.Next(1900000000ULL);

    EXPECT_EQ(first.pts_ns, 0ULL);
    EXPECT_EQ(second.pts_ns, 33333333ULL);
    EXPECT_EQ(second.source_status, SourceTimestampStatus::REGRESSED);
}

/**
 * @brief 验证 30 帧边界不会累积整数除法误差
 * @return 无
 * @throws 不抛出异常
 */
TEST(MediaTimelineTest, ThirtyFramesEndAtOneSecond)
{
    MediaTimeline timeline;
    MediaFrameTiming timing;
    for (uint64_t frame_index = 0; frame_index <= 30; ++frame_index)
    {
        timing = timeline.Next(3000000000ULL + frame_index * 33333333ULL);
    }

    EXPECT_EQ(timing.pts_ns, 1000000000ULL);
}

}  // namespace dexe_recorder
