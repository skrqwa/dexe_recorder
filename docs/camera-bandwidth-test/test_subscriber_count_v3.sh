#!/usr/bin/env bash
# 多订阅者频率影响测试 v3（Python 真实订阅者）
# 用 fake_subscriber.py 完整接收消息模拟真实录制节点
# 用法: ./test_subscriber_count_v3.sh [pc1|pc2|all]

DURATION=12
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FAKE_SUB="$SCRIPT_DIR/fake_subscriber.py"
SCOPE="${1:-all}"

source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20

measure() {
  local topic=$1
  local msg_type=$2
  local extra=$3
  local label=$4
  local machine=$5
  local sub_pids=()

  for i in $(seq 1 $extra); do
    if [ "$machine" = "pc2" ]; then
      scp -q "$FAKE_SUB" dexforce@192.168.20.21:/tmp/fake_subscriber.py 2>/dev/null
      ssh -o ConnectTimeout=3 dexforce@192.168.20.21 \
        "source /opt/ros/humble/setup.bash; export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; export ROS_DOMAIN_ID=20; \
         timeout $((DURATION+5)) python3 /tmp/fake_subscriber.py $topic $msg_type $((DURATION+3))" &
      sub_pids+=($!)
    else
      timeout $((DURATION+5)) python3 "$FAKE_SUB" "$topic" "$msg_type" $((DURATION+3)) &
      sub_pids+=($!)
    fi
  done

  sleep 3

  echo "[$label] ${extra}个py订阅者, 测量 ${DURATION}s..."
  ros2 topic hz "$topic" 2>&1 & HZ_PID=$!
  sleep $DURATION
  kill $HZ_PID 2>/dev/null
  wait $HZ_PID 2>/dev/null
  echo

  for pid in "${sub_pids[@]}"; do
    kill $pid 2>/dev/null || true
  done
  wait 2>/dev/null || true
  sleep 2
}

echo "=========================================="
echo "多订阅者频率影响测试 v3 (Python真实订阅者)"
echo "时间: $(date)"
echo "基线: resize=1订阅者, compressed=2订阅者"
echo "=========================================="
echo

if [ "$SCOPE" = "all" ] || [ "$SCOPE" = "pc1" ]; then
  echo "########## PC1 本地 ##########"
  echo
  for n in 0 1 2 3; do
    echo "--- PC1 resize, +${n} py (总$((n+1))) ---"
    measure /camera/left_eye_resize image $n "PC1-resize+${n}" pc1
  done
  for n in 0 1 2 3; do
    echo "--- PC1 compressed, +${n} py (总$((n+2))) ---"
    measure /camera/kfc_compressed compressed $n "PC1-comp+${n}" pc1
  done
fi

if [ "$SCOPE" = "all" ] || [ "$SCOPE" = "pc2" ]; then
  echo "########## PC2 跨机 ##########"
  echo
  for n in 0 1 2 3; do
    echo "--- PC2 跨机 resize, +${n} py (总$((n+1))) ---"
    measure /camera/left_eye_resize image $n "PC2-resize+${n}" pc2
  done
  for n in 0 1 2 3; do
    echo "--- PC2 跨机 compressed, +${n} py (总$((n+2))) ---"
    measure /camera/kfc_compressed compressed $n "PC2-comp+${n}" pc2
  done
fi

echo "=========================================="
echo "测试完成"
echo "=========================================="
