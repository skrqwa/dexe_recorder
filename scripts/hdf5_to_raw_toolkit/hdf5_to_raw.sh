#!/bin/bash
# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
# hdf5_to_raw.sh - 将 HDF5 文件还原为原始录制数据结构
#
# 用法:
#   ./hdf5_to_raw.sh <hdf5文件> <输出目录>          # 单文件
#   ./hdf5_to_raw.sh <hdf5目录> <输出目录>           # 批量
#
# 示例:
#   ./hdf5_to_raw.sh data.hdf5 output/
#   ./hdf5_to_raw.sh hdf5_dir/ output/
#
# 依赖: python3, h5py, numpy, Pillow, av(PyAV)
#   pip install h5py numpy Pillow av
# ----------------------------------------------------------------------------

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -lt 2 ]; then
    echo "用法: $0 <hdf5文件或目录> <输出目录>"
    echo ""
    echo "示例:"
    echo "  $0 data.hdf5 output/       # 单文件"
    echo "  $0 hdf5_dir/ output/       # 批量"
    exit 1
fi

INPUT="$1"
OUTPUT="$2"

if [ -f "$INPUT" ]; then
    python3 "$SCRIPT_DIR/hdf5_to_raw.py" --input "$INPUT" --output "$OUTPUT"
elif [ -d "$INPUT" ]; then
    python3 "$SCRIPT_DIR/hdf5_to_raw.py" --input-dir "$INPUT" --output "$OUTPUT"
else
    echo "错误: 输入路径不存在: $INPUT"
    exit 1
fi
