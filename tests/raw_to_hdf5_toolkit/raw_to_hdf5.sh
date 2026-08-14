#!/bin/bash
# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
# raw_to_hdf5.sh - 将原始录制数据（图片 + pose_record）转换为视频 HDF5
#
# 用法:
#   ./raw_to_hdf5.sh <输入路径> <输出目录> [video|jpeg] [选项]
#
# 示例:
#   # 单个 session 目录
#   ./raw_to_hdf5.sh data/recorded/test2/20260617_122549 data/hdf5 video
#
#   # 批量转换（输入为包含多个 session 的目录）
#   ./raw_to_hdf5.sh data/recorded/test2 data/hdf5 video
#
#   # 跳过已存在文件
#   ./raw_to_hdf5.sh data/recorded/test2 data/hdf5 video --skip-existing
#
# 依赖（Jetson Orin NX 环境）:
#   - Python 3.10+
#   - GStreamer 1.0 + nvv4l2h264enc（Jetson 硬件编码器，系统自带）
#   - Python 包: h5py numpy opencv-python Pillow tqdm pyyaml
#     pip install h5py numpy opencv-python Pillow tqdm pyyaml
# ----------------------------------------------------------------------------

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -lt 2 ]; then
    echo "用法: $0 <输入路径> <输出目录> [video|jpeg] [额外选项]"
    echo ""
    echo "示例:"
    echo "  $0 data/recorded/test2/20260617_122549 data/hdf5 video"
    echo "  $0 data/recorded/test2 data/hdf5 video --skip-existing"
    echo "  $0 data/recorded/test2 data/hdf5 jpeg"
    exit 1
fi

INPUT="$1"
OUTPUT="$2"
FMT="${3:-video}"
shift 3 2>/dev/null || shift $#  # 剩余参数透传给 Python 脚本

if [ ! -e "$INPUT" ]; then
    echo "错误: 输入路径不存在: $INPUT"
    exit 1
fi

PYTHON="${PYTHON:-python3}"

exec "$PYTHON" "$SCRIPT_DIR/raw_to_hdf5.py" \
    --input "$INPUT" \
    --output "$OUTPUT" \
    --format "$FMT" \
    "$@"
