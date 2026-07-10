#!/usr/bin/env bash
# 网络延迟测试：不同订阅者数量下 PC1-PC2 的 ping RTT 变化
# 用法: ./test_network_latency.sh

DURATION=12
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FAKE_SUB="$SCRIPT_DIR/fake_subscriber.py"

source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20

measure_latency() {
  local topic=$1
  local msg_type=$2
  local extra=$3
  local label=$4
  local sub_pids=()

  # 启动额外订阅者（PC2 上）
  for i in $(seq 1 $extra); do
    ssh -o ConnectTimeout=3 dexforce@192.168.20.21 \
      "source /opt/ros/humble/setup.bash; export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; export ROS_DOMAIN_ID=20; \
       timeout $((DURATION+5)) python3 /tmp/fake_subscriber.py $topic $msg_type $((DURATION+3))" &
    sub_pids+=($!)
  done

  sleep 3

  # 同时测 ping 和 topic hz
  echo "[$label] ${extra}个PC2订阅者, 测量 ping RTT + topic hz (${DURATION}s)..."
  echo "--- ping ---"
  # 发 10 个 ping，间隔 1 秒
  ping -c 10 -i 1 192.168.20.21 2>&1 | tail -3
  echo "--- hz ---"
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

# 确保 PC2 有 fake_subscriber.py
scp -q "$FAKE_SUB" dexforce@192.168.20.21:/tmp/fake_subscriber.py 2>/dev/null

echo "=========================================="
echo "网络延迟测试 (PC1->PC2 ping RTT)"
echo "时间: $(date)"
echo "=========================================="
echo

echo "### 基线 ping（无额外订阅者）###"
ping -c 10 -i 1 192.168.20.21 2>&1 | tail -3
echo

echo "########## resize 话题 ##########"
for n in 0 1 2 3; do
  echo "--- resize, +${n} PC2订阅者 ---"
  measure_latency /camera/left_eye_resize image $n "resize+${n}"
done

echo "########## compressed 话题 ##########"
for n in 0 1 2 3; do
  echo "--- compressed, +${n} PC2订阅者 ---"
  measure_latency /camera/kfc_compressed compressed $n "comp+${n}"
done

echo "=========================================="
echo "测试完成"
echo "=========================================="
