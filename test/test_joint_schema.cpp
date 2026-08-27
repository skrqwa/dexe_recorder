// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#include <gtest/gtest.h>

#include "dexe_recorder/joint_schema.h"

namespace dexe_recorder
{

/**
 * @brief 验证缺少 joint_name 的旧版 W1 20 维数组使用已确认的固定顺序
 * @return 无
 * @throws 不抛出异常
 */
TEST(JointSchemaTest, MissingNamesUseLegacyW1TwentyJointOrder)
{
    bool used_legacy_fallback = false;
    std::string error;

    const auto names = ResolveStateJointNames({}, 20, &used_legacy_fallback, &error);

    const std::vector<std::string> expected = {
        "ANKLE",    "KNEE",     "BUTTOCK",  "WAIST",    "NECK1",    "NECK2",    "LEFT_J1",
        "LEFT_J2",  "LEFT_J3",  "LEFT_J4",  "LEFT_J5",  "LEFT_J6",  "LEFT_J7",  "RIGHT_J1",
        "RIGHT_J2", "RIGHT_J3", "RIGHT_J4", "RIGHT_J5", "RIGHT_J6", "RIGHT_J7",
    };
    EXPECT_EQ(names, expected);
    EXPECT_TRUE(used_legacy_fallback);
    EXPECT_TRUE(error.empty());
}

/**
 * @brief 验证新格式消息显式提供的关节名称保持不变
 * @return 无
 * @throws 不抛出异常
 */
TEST(JointSchemaTest, SuppliedNamesArePreserved)
{
    bool used_legacy_fallback = true;
    std::string error;
    const std::vector<std::string> supplied = {"JOINT_A", "JOINT_B"};

    const auto names = ResolveStateJointNames(supplied, 2, &used_legacy_fallback, &error);

    EXPECT_EQ(names, supplied);
    EXPECT_FALSE(used_legacy_fallback);
    EXPECT_TRUE(error.empty());
}

/**
 * @brief 验证未确认的无名称关节维度不会被猜测映射
 * @return 无
 * @throws 不抛出异常
 */
TEST(JointSchemaTest, UnknownUnnamedJointCountIsRejected)
{
    bool used_legacy_fallback = false;
    std::string error;

    const auto names = ResolveStateJointNames({}, 14, &used_legacy_fallback, &error);

    EXPECT_TRUE(names.empty());
    EXPECT_FALSE(used_legacy_fallback);
    EXPECT_FALSE(error.empty());
}

/**
 * @brief 验证显式名称数量与数值数量不一致时拒绝解析
 * @return 无
 * @throws 不抛出异常
 */
TEST(JointSchemaTest, SuppliedNameCountMismatchIsRejected)
{
    bool used_legacy_fallback = false;
    std::string error;

    const auto names = ResolveStateJointNames({"JOINT_A"}, 2, &used_legacy_fallback, &error);

    EXPECT_TRUE(names.empty());
    EXPECT_FALSE(used_legacy_fallback);
    EXPECT_FALSE(error.empty());
}

}  // namespace dexe_recorder
