#!/usr/bin/env bash
# compressed 话题极限压测：5/10/15/20 个订阅者
# 用法: ./test_compressed_limit.sh

DURATION=12
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FAKE_SUB="$SCRIPT_DIR/fake_subscriber.py"

source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20

measure() {
  local extra=$1
  local label=$2
  local sub_pids=()

  # PC1 本地订阅者
  for i in $(seq 1 $extra); do
    timeout $((DURATION+5)) python3 "$FAKE_SUB" /camera/kfc_compressed compressed $((DURATION+3)) > /dev/null 2>&1 &
    sub_pids+=($!)
  done

  sleep 3
  echo "[$label] ${extra}个本地订阅者, 测量 ${DURATION}s..."
  echo "--- ping ---"
  ping -c 5 -i 1 192.168.20.21 2>&1 | tail -2
  echo "--- hz ---"
  ros2 topic hz /camera/kfc_compressed 2>&1 & HZ_PID=$!
  sleep $DURATION
  kill $HZ_PID 2>/dev/null
  wait $HZ_PID 2>/dev/null
  echo "--- bw ---"
  ros2 topic bw /camera/kfc_compressed 2>&1 & BW_PID=$!
  sleep 5
  kill $BW_PID 2>/dev/null
  wait $BW_PID 2>/dev/null
  echo

  for pid in "${sub_pids[@]}"; do
    kill $pid 2>/dev/null || true
  done
  wait 2>/dev/null || true
  sleep 2
}

echo "=========================================="
echo "compressed 话题极限压测"
echo "时间: $(date)"
echo "基线: 2 个已有订阅者"
echo "=========================================="
echo

for n in 3 8 13 18 28; do
  echo "########## +${n} 订阅者 (总$((n+2))) ##########"
  measure $n "comp+${n}"
done

echo "=========================================="
echo "压测完成"
echo "=========================================="
