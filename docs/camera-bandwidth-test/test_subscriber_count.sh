#!/usr/bin/env bash
# 多订阅者频率影响测试
# 测不同订阅者数量(1/2/3/4)下，resize 和 compressed 话题在 PC1/PC2 上的频率
# 用法: ./test_subscriber_count.sh

DURATION=12  # 每个场景测 12 秒
source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20

# 测某一话题在指定额外订阅者数量下的频率
# $1=话题 $2=额外订阅者数 $3=标签 $4=是否跨机(pc1/pc2)
measure_hz_with_subs() {
  local topic=$1
  local extra_subs=$2
  local label=$3
  local machine=$4
  local sub_pids=()

  # 启动额外订阅者（用 ros2 topic hz 作为订阅者，既占订阅又可忽略输出）
  for i in $(seq 1 $extra_subs); do
    if [ "$machine" = "pc2" ]; then
      ssh -o ConnectTimeout=3 dexforce@192.168.20.21 \
        "source /opt/ros/humble/setup.bash; export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; export ROS_DOMAIN_ID=20; \
         timeout $((DURATION+5)) ros2 topic hz $topic > /dev/null 2>&1" &
      sub_pids+=($!)
    else
      timeout $((DURATION+5)) ros2 topic hz "$topic" > /dev/null 2>&1 &
      sub_pids+=($!)
    fi
  done

  sleep 2  # 等订阅建立

  # 在 PC1 上测频率
  echo "[$label] ${extra_subs}个额外订阅者, 测量 ${DURATION}s..."
  ros2 topic hz "$topic" 2>&1 & HZ_PID=$!
  sleep $DURATION
  kill $HZ_PID 2>/dev/null
  wait $HZ_PID 2>/dev/null

  # 清理订阅者
  for pid in "${sub_pids[@]}"; do
    kill $pid 2>/dev/null || true
  done
  wait 2>/dev/null || true
  sleep 1
}

echo "=========================================="
echo "多订阅者频率影响测试"
echo "时间: $(date)"
echo "auto 模式基线订阅者:"
echo "  left_eye_resize: 1 (w1_act_flexible_node)"
echo "  right_eye_resize: 1 (w1_act_flexible_node)"
echo "  kfc_compressed: 2 (infer_server_head_camera_sub, image_getter)"
echo "=========================================="
echo

# ===== PC1 本地测试 =====
echo "########## PC1 本地 ##########"
echo

for n in 0 1 2 3; do
  echo "--- PC1 resize, 额外${n}订阅者(总$((n+1))个) ---"
  measure_hz_with_subs /camera/left_eye_resize $n "PC1-resize-subs${n}" pc1
  echo
done

for n in 0 1 2 3; do
  echo "--- PC1 compressed, 额外${n}订阅者(总$((n+2))个) ---"
  measure_hz_with_subs /camera/kfc_compressed $n "PC1-compressed-subs${n}" pc1
  echo
done

# ===== PC2 跨机测试 =====
echo "########## PC2 跨机 ##########"
echo

for n in 0 1 2 3; do
  echo "--- PC2 跨机 resize, PC2额外${n}订阅者(总$((n+1))个) ---"
  measure_hz_with_subs /camera/left_eye_resize $n "PC2-resize-subs${n}" pc2
  echo
done

for n in 0 1 2 3; do
  echo "--- PC2 跨机 compressed, PC2额外${n}订阅者(总$((n+2))个) ---"
  measure_hz_with_subs /camera/kfc_compressed $n "PC2-compressed-subs${n}" pc2
  echo
done

echo "=========================================="
echo "测试完成"
echo "=========================================="
