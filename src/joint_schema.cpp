// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#include "dexe_recorder/joint_schema.h"

namespace dexe_recorder
{

/**
 * @brief 解析状态消息中的关节名称，并兼容已确认的 W1 旧版 20 关节数组顺序
 * @param supplied_names 消息显式提供的关节名称
 * @param value_count 关节数值数组长度
 * @param used_legacy_fallback 输出是否使用了旧版 W1 20 关节映射
 * @param error 输出失败原因；成功时为空
 * @return 与数值数组一一对应的关节名称；失败时为空
 * @throws std::bad_alloc 分配返回值或错误字符串失败
 */
std::vector<std::string> ResolveStateJointNames(const std::vector<std::string>& supplied_names,
                                                std::size_t value_count,
                                                bool* used_legacy_fallback,
                                                std::string* error)
{
    // W1 旧版 robot_server_state 不发布 joint_name。
    // 数值顺序由 robot_server 与 joint converter 共同契约确认。
    static const std::vector<std::string> kLegacyW1TwentyJointOrder = {
        "ANKLE",    "KNEE",     "BUTTOCK",  "WAIST",    "NECK1",    "NECK2",    "LEFT_J1",
        "LEFT_J2",  "LEFT_J3",  "LEFT_J4",  "LEFT_J5",  "LEFT_J6",  "LEFT_J7",  "RIGHT_J1",
        "RIGHT_J2", "RIGHT_J3", "RIGHT_J4", "RIGHT_J5", "RIGHT_J6", "RIGHT_J7",
    };

    if (used_legacy_fallback != nullptr)
    {
        *used_legacy_fallback = false;
    }
    if (error != nullptr)
    {
        error->clear();
    }
    if (supplied_names.empty())
    {
        if (value_count == kLegacyW1TwentyJointOrder.size())
        {
            if (used_legacy_fallback != nullptr)
            {
                *used_legacy_fallback = true;
            }
            return kLegacyW1TwentyJointOrder;
        }
        if (error != nullptr)
        {
            *error = "joint_name missing; unnamed count is not the confirmed W1 20-joint schema";
        }
        return {};
    }
    if (supplied_names.size() != value_count)
    {
        if (error != nullptr)
        {
            *error = "joint_name count does not match joint value count";
        }
        return {};
    }
    return supplied_names;
}

}  // namespace dexe_recorder
