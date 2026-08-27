// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#pragma once

#include <cstddef>
#include <string>
#include <vector>

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
                                                std::string* error);

}  // namespace dexe_recorder
