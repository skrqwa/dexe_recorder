#!/bin/bash
# 录制数据转 HDF5 脚本（傻瓜式部署）
# 用法: ./convert_to_hdf5.sh [format] [convert_to_hdf5.py 其他参数]
#   format: video / jpeg / auto（默认 auto 自动检测）
#
# 默认输入: workspace/dexe_recorder/data（录制数据）
# 默认输出: workspace/dexe_recorder/hdf5（HDF5 文件）
#
# 示例:
#   ./convert_to_hdf5.sh              # 自动检测格式
#   ./convert_to_hdf5.sh video        # 指定 video 格式
#
# 依赖: h5py, numpy, PyAV（VIDEO 模式）, Pillow（JPEG 模式）, tqdm（可选）
# VIDEO 对齐/修复在 Jetson 上还需 PyGObject + GStreamer nvv4l2h264enc。

set -e

RECORDER_DIR="/home/dexforce/workspace/dexe_recorder"
RECORDED_DIR="${RECORDER_DIR}/data"
OUTPUT_DIR="${RECORDER_DIR}/hdf5"
FORMAT="${1:-auto}"
if [ "$#" -gt 0 ]; then
  shift
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONVERT_SCRIPT="${SCRIPT_DIR}/convert_to_hdf5.py"

if [ ! -f "$CONVERT_SCRIPT" ]; then
  echo "[ERROR] 找不到 convert_to_hdf5.py，应在同目录下"
  exit 1
fi

echo "[INFO] 输入目录: $RECORDED_DIR"
echo "[INFO] 输出目录: $OUTPUT_DIR"
echo "[INFO] 格式: $FORMAT"
echo ""

python3 "$CONVERT_SCRIPT" \
  --input "$RECORDED_DIR" \
  --output "$OUTPUT_DIR" \
  --format "$FORMAT" \
  --skip-existing \
  "$@"
