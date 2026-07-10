#!/bin/bash
# dexe_recorder 服务调用脚本（PC1/PC2 通用）
# 用法: ./recorderctl.sh [start|stop|status]
#
# 示例:
#   ./recorderctl.sh start    # 开始录制
#   ./recorderctl.sh stop     # 停止录制
#   ./recorderctl.sh status   # 查询状态

source /opt/ros/humble/setup.bash

# 自动检测 ROS install 路径（PC1: ~/w1/install, PC2: ~/workspace/install）
if [ -f "$HOME/w1/install/setup.bash" ]; then
  source "$HOME/w1/install/setup.bash"
elif [ -f "$HOME/workspace/install/setup.bash" ]; then
  source "$HOME/workspace/install/setup.bash"
else
  echo "[ERROR] 找不到 ROS install setup.bash"
  exit 1
fi

export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20

CMD="${1:-status}"

case "$CMD" in
  start)
    ros2 service call /dexe_recorder/start_recording std_srvs/srv/Trigger
    ;;
  stop)
    ros2 service call /dexe_recorder/stop_recording std_srvs/srv/Trigger
    ;;
  status)
    ros2 service call /dexe_recorder/get_status std_srvs/srv/Trigger
    ;;
  *)
    echo "用法: $0 [start|stop|status]"
    exit 1
    ;;
esac
