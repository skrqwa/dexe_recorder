#!/usr/bin/env bash
# dexe_recorder 服务调用脚本（PC1/PC2 通用）
# 用法: ./recorderctl.sh [start|stop|status]

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
SERVICE_TIMEOUT_SEC="${SERVICE_TIMEOUT_SEC:-10}"

log() {
  local level="$1"
  shift
  printf '[%s] [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$level" "$*"
}

if [ ! -f "$INSTALL_SETUP" ]; then
  log ERROR "找不到工作区环境: $INSTALL_SETUP"
  exit 1
fi

source /opt/ros/humble/setup.bash
source "$INSTALL_SETUP"
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><NetworkInterfaceAddress>192.168.20.21</NetworkInterfaceAddress><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'

CMD="${1:-status}"
case "$CMD" in
  start) SERVICE="/dexe_recorder/start_recording" ;;
  stop) SERVICE="/dexe_recorder/stop_recording" ;;
  status) SERVICE="/dexe_recorder/get_status" ;;
  *)
    log ERROR "用法: $0 [start|stop|status]"
    exit 1
    ;;
esac

log INFO "调用服务: $SERVICE（超时 ${SERVICE_TIMEOUT_SEC}s）"
if ! timeout "${SERVICE_TIMEOUT_SEC}s" ros2 service call "$SERVICE" std_srvs/srv/Trigger; then
  log ERROR "服务不可用或调用超时: $SERVICE；请检查 $RECORDER_DIR/log/dexe_recorder.log"
  exit 1
fi
