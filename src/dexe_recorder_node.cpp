// ----------------------------------------------------------------------------
// Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
//
// All rights reserved.
// ----------------------------------------------------------------------------
#include "dexe_recorder/recorder_node.h"

#include <memory>

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  auto node = std::make_shared<dexe_recorder::RecorderNode>();
  rclcpp::spin(node);
  rclcpp::shutdown();
  return 0;
}
