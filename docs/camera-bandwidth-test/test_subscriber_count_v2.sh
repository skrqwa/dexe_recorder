#!/usr/bin/env bash
# 多订阅者频率影响测试 v2（用 ros2 topic echo 完整接收数据）
# 测不同订阅者数量下，resize 和 compressed 话题在 PC1/PC2 上的频率
# 用法: ./test_subscriber_count_v2.sh [pc1|pc2|all]

DURATION=12
source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20

SCOPE="${1:-all}"

measure() {
  local topic=$1
  local extra=$2
  local label=$3
  local machine=$4
  local sub_pids=()

  # 启动额外订阅者（ros2 topic echo 完整接收数据，真实占带宽）
  for i in $(seq 1 $extra); do
    if [ "$machine" = "pc2" ]; then
      ssh -o ConnectTimeout=3 dexforce@192.168.20.21 \
        "source /opt/ros/humble/setup.bash; export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; export ROS_DOMAIN_ID=20; \
         timeout $((DURATION+5)) ros2 topic echo $topic sensor_msgs/msg/Image --field header.stamp > /dev/null 2>&1 || \
         timeout $((DURATION+5)) ros2 topic echo $topic sensor_msgs/msg/CompressedImage --field header.stamp > /dev/null 2>&1" &
      sub_pids+=($!)
    else
      # 尝试 Image 类型，失败则 CompressedImage
      timeout $((DURATION+5)) ros2 topic echo "$topic" sensor_msgs/msg/Image --field header.stamp > /dev/null 2>&1 &
      sub_pids+=($!)
    fi
  done

  sleep 3  # 等订阅建立

  # 测频率（测量者独立于额外订阅者）
  echo "[$label] ${extra}个echo订阅者, 测量 ${DURATION}s..."
  ros2 topic hz "$topic" 2>&1 & HZ_PID=$!
  sleep $DURATION
  kill $HZ_PID 2>/dev/null
  wait $HZ_PID 2>/dev/null
  echo

  # 清理
  for pid in "${sub_pids[@]}"; do
    kill $pid 2>/dev/null || true
  done
  wait 2>/dev/null || true
  sleep 2
}

echo "=========================================="
echo "多订阅者频率影响测试 v2 (ros2 topic echo)"
echo "时间: $(date)"
echo "基线: resize=1订阅者, compressed=2订阅者"
echo "=========================================="
echo

if [ "$SCOPE" = "all" ] || [ "$SCOPE" = "pc1" ]; then
  echo "########## PC1 本地 ##########"
  echo
  for n in 0 1 2 3; do
    echo "--- PC1 resize, +${n} echo (总$((n+1))) ---"
    measure /camera/left_eye_resize $n "PC1-resize+${n}" pc1
  done
  for n in 0 1 2 3; do
    echo "--- PC1 compressed, +${n} echo (总$((n+2))) ---"
    measure /camera/kfc_compressed $n "PC1-comp+${n}" pc1
  done
fi

if [ "$SCOPE" = "all" ] || [ "$SCOPE" = "pc2" ]; then
  echo "########## PC2 跨机 ##########"
  echo
  for n in 0 1 2 3; do
    echo "--- PC2 跨机 resize, +${n} echo (总$((n+1))) ---"
    measure /camera/left_eye_resize $n "PC2-resize+${n}" pc2
  done
  for n in 0 1 2 3; do
    echo "--- PC2 跨机 compressed, +${n} echo (总$((n+2))) ---"
    measure /camera/kfc_compressed $n "PC2-comp+${n}" pc2
  done
fi

echo "=========================================="
echo "测试完成"
echo "=========================================="
