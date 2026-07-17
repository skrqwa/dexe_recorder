#!/bin/bash
# dexe_recorder 启动脚本（傻瓜式部署）
# 用法: ./run_recorder.sh
# 如果已有实例运行，会先杀掉旧实例再启动新的

set -e

# 检测并杀掉已有实例（含 ros2 run launcher 和节点进程）
EXISTING_PIDS=$(pgrep -f "dexe_recorder_node" 2>/dev/null)
if [ -n "$EXISTING_PIDS" ]; then
  echo "[INFO] 发现已有实例运行 (PID=$EXISTING_PIDS)，正在停止..."
  echo "$EXISTING_PIDS" | xargs kill 2>/dev/null
  sleep 2
  # 如果还没退出，强制杀所有
  echo "$EXISTING_PIDS" | xargs kill -9 2>/dev/null || true
  echo "[INFO] 旧实例已停止"
fi

source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><NetworkInterfaceAddress>192.168.20.21</NetworkInterfaceAddress><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'

source /home/dexforce/workspace/install/setup.bash

# 数据和日志都放在 workspace/dexe_recorder 下
RECORDER_DIR="/home/dexforce/workspace/dexe_recorder"
mkdir -p "$RECORDER_DIR/data" "$RECORDER_DIR/log"
cd "$RECORDER_DIR"

echo "[INFO] dexe_recorder_node 启动中..."
ros2 run dexe_recorder dexe_recorder_node > "$RECORDER_DIR/log/dexe_recorder.log" 2>&1 &
NEW_PID=$!
sleep 2
if kill -0 "$NEW_PID" 2>/dev/null; then
  echo "[INFO] dexe_recorder_node 启动成功 (PID=$NEW_PID)，日志: $RECORDER_DIR/log/dexe_recorder.log"
else
  echo "[ERROR] dexe_recorder_node 启动失败，查看日志: $RECORDER_DIR/log/dexe_recorder.log"
  exit 1
fi
wait "$NEW_PID"
