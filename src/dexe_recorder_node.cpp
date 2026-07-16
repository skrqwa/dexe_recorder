// ----------------------------------------------------------------------------
// Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
/**
 * @file dexe_recorder_node.cpp
 * @brief dexe_recorder 节点入口
 *
 * 初始化 ROS2，创建 RecorderNode 节点并 spin。
 */
#include <memory>

#include "dexe_recorder/recorder_node.h"

/**
 * @brief 主函数
 * @param argc 参数个数
 * @param argv 参数数组
 * @return 程序退出码
 */
int main(int argc, char** argv)
{
    rclcpp::init(argc, argv);
    auto node = std::make_shared<dexe_recorder::RecorderNode>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
