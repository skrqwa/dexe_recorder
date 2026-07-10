#!/usr/bin/env bash
# 相机话题跨机订阅带宽与频率实测脚本
# 用法: ./test_camera_bandwidth.sh [场景]
#   场景1: pc1_local  - PC1 本地订阅头部 resize（基线）
#   场景2: pc2_cross  - PC2 跨机订阅 PC1 的头部 resize
#   场景3: pc2_compressed - PC2 跨机订阅 PC1 的 kfc_compressed
# 默认: 全部测

set -e

SCENE="${1:-all}"
DURATION=10  # 每个场景测 10 秒

source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20

measure_hz() {
  local topic=$1
  local label=$2
  echo "[$label] 测量 $topic 频率 (${DURATION}s)..."
  timeout $DURATION ros2 topic hz "$topic" 2>&1 | tail -5 || true
  echo
}

measure_bw() {
  local topic=$1
  local label=$2
  echo "[$label] 测量 $topic 带宽 (${DURATION}s)..."
  timeout $DURATION ros2 topic bw "$topic" 2>&1 | tail -5 || true
  echo
}

echo "=========================================="
echo "相机话题跨机订阅实测"
echo "时间: $(date)"
echo "时长: 每场景 ${DURATION}s"
echo "=========================================="
echo

if [ "$SCENE" = "all" ] || [ "$SCENE" = "pc1_local" ]; then
  echo "### 场景1: PC1 本地订阅头部 resize（基线）###"
  echo "在 PC1 上测，订阅者已有(w1_act)，测当前频率与带宽"
  measure_hz /camera/left_eye_resize "PC1本地-left_resize"
  measure_bw /camera/left_eye_resize "PC1本地-left_resize"
  measure_bw /camera/kfc_compressed "PC1本地-kfc_compressed"
  echo
fi

if [ "$SCENE" = "all" ] || [ "$SCENE" = "pc2_cross" ]; then
  echo "### 场景2: PC2 跨机订阅 PC1 的头部 resize ###"
  echo "在 PC2 上启动订阅，同时在 PC1 测频率变化"
  echo "[PC2] 启动跨机订阅 left_eye_resize..."
  ssh -o ConnectTimeout=3 dexforce@192.168.20.21 \
    "source /opt/ros/humble/setup.bash; export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; export ROS_DOMAIN_ID=20; \
     timeout $((DURATION+3)) ros2 topic echo /camera/left_eye_resize --no-arr > /dev/null 2>&1" &
  SSH_PID=$!
  sleep 2
  echo "[PC1] 测量 left_eye_resize 频率（跨机订阅后）..."
  timeout $DURATION ros2 topic hz /camera/left_eye_resize 2>&1 | tail -3 || true
  echo "[PC1] 测量 left_eye_resize 带宽（跨机订阅后）..."
  timeout $DURATION ros2 topic bw /camera/left_eye_resize 2>&1 | tail -3 || true
  wait $SSH_PID 2>/dev/null || true
  echo
fi

if [ "$SCENE" = "all" ] || [ "$SCENE" = "pc2_compressed" ]; then
  echo "### 场景3: PC2 跨机订阅 PC1 的 kfc_compressed ###"
  echo "[PC2] 启动跨机订阅 kfc_compressed..."
  ssh -o ConnectTimeout=3 dexforce@192.168.20.21 \
    "source /opt/ros/humble/setup.bash; export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; export ROS_DOMAIN_ID=20; \
     timeout $((DURATION+3)) ros2 topic echo /camera/kfc_compressed --no-arr > /dev/null 2>&1" &
  SSH_PID=$!
  sleep 2
  echo "[PC1] 测量 kfc_compressed 频率（跨机订阅后）..."
  timeout $DURATION ros2 topic hz /camera/kfc_compressed 2>&1 | tail -3 || true
  echo "[PC1] 测量 kfc_compressed 带宽（跨机订阅后）..."
  timeout $DURATION ros2 topic bw /camera/kfc_compressed 2>&1 | tail -3 || true
  wait $SSH_PID 2>/dev/null || true
  echo
fi

echo "=========================================="
echo "实测完成"
echo "=========================================="
