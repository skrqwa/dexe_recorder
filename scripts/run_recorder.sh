#!/usr/bin/env bash
# dexe_recorder 启动脚本（PC1/PC2 通用）
# 用法: ./run_recorder.sh

set -eo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/config/auto_recorder.yaml" ]; then
  RECORDER_DIR="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../config/auto_recorder.yaml" ]; then
  RECORDER_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
else
  printf '[%s] [ERROR] 无法从脚本位置找到 config/auto_recorder.yaml\n' "$(date '+%Y-%m-%d %H:%M:%S')" >&2
  exit 1
fi

WORKSPACE_DIR="$(cd -- "$RECORDER_DIR/.." && pwd)"
INSTALL_SETUP="$WORKSPACE_DIR/install/setup.bash"
LOG_DIR="$RECORDER_DIR/log"
LOG_FILE="$LOG_DIR/dexe_recorder.log"
RAW_LOG_FILE="$LOG_DIR/dexe_recorder.raw.log"

log() {
  local level="$1"
  shift
  printf '[%s] [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$level" "$*"
}

# pgrep 在没有匹配进程时返回 1；这里必须显式允许，避免 set -e 提前退出。
EXISTING_PIDS="$(pgrep -f '[d]exe_recorder_node' 2>/dev/null || true)"
if [ -n "$EXISTING_PIDS" ]; then
  log INFO "发现已有实例运行 (PID=$EXISTING_PIDS)，正在停止..."
  echo "$EXISTING_PIDS" | xargs kill 2>/dev/null || true
  sleep 2
  echo "$EXISTING_PIDS" | xargs kill -9 2>/dev/null || true
  log INFO "旧实例已停止"
fi

if [ ! -f "$INSTALL_SETUP" ]; then
  log ERROR "找不到工作区环境: $INSTALL_SETUP"
  exit 1
fi

if [ ! -f "$RECORDER_DIR/config/auto_recorder.yaml" ]; then
  log ERROR "找不到录制配置: $RECORDER_DIR/config/auto_recorder.yaml"
  exit 1
fi

source /opt/ros/humble/setup.bash
source "$INSTALL_SETUP"
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><NetworkInterfaceAddress>192.168.20.21</NetworkInterfaceAddress><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'
export RCUTILS_LOGGING_BUFFERED_STREAM=0
export RCUTILS_COLORIZED_OUTPUT=0

mkdir -p "$RECORDER_DIR/data" "$LOG_DIR"
cd "$RECORDER_DIR"

log INFO "dexe_recorder_node 启动中，环境: $INSTALL_SETUP"
printf '[%s] [INFO] ===== dexe_recorder_node new launch =====\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"

# 节点直接写 raw 日志，避免管道改变其缓冲行为；tail 负责追加可读时间。
(
  : > "$RAW_LOG_FILE"
  ros2 run dexe_recorder dexe_recorder_node >> "$RAW_LOG_FILE" 2>&1 &
  NODE_PID=$!
  stdbuf -oL tail --pid="$NODE_PID" -s 0.2 -n +1 -F "$RAW_LOG_FILE" 2>/dev/null |
    while IFS= read -r line; do
      printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$line"
    done >> "$LOG_FILE"
  wait "$NODE_PID"
) &
NEW_PID=$!

sleep 2
if kill -0 "$NEW_PID" 2>/dev/null; then
  log INFO "dexe_recorder_node 启动成功 (PID=$NEW_PID)，日志: $LOG_FILE"
else
  log ERROR "dexe_recorder_node 启动失败，最近日志如下:"
  tail -n 40 "$LOG_FILE" >&2 || true
  exit 1
fi

wait "$NEW_PID"
