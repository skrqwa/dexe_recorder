#!/bin/bash
# dexe_recorder 部署脚本（在 PC2 上执行）
# 前提：dexe_recorder 包已放在 ~/workspace/install/dexe_recorder/ 下
#
# 用法:
#   cd ~/workspace/install/dexe_recorder/share/dexe_recorder
#   ./deploy.sh
#
# 部署后目录结构：
#   ~/workspace/dexe_recorder/
#   ├── run_recorder.sh        # 启动录制
#   ├── convert_to_hdf5.sh     # 转 HDF5
#   ├── convert_to_hdf5.py     # 转换脚本
#   ├── config/
#   │   └── auto_recorder.yaml # 配置文件（可修改）
#   ├── data/                   # 录制数据（自动创建）
#   ├── hdf5/                   # HDF5 文件（自动创建）
#   └── log/                    # 日志（自动创建）

set -e

# 找到包 share 目录（本脚本所在目录）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 如果从 install/dexe_recorder/share/dexe_recorder/ 执行，SCRIPT_DIR 就是 share 目录
# 如果从 workspace/dexe_recorder/ 执行，则从 install 目录查找
if [ ! -f "$SCRIPT_DIR/run_recorder.sh" ]; then
  # 尝试从 install 目录查找
  INSTALL_SHARE="$HOME/workspace/install/dexe_recorder/share/dexe_recorder"
  if [ -f "$INSTALL_SHARE/run_recorder.sh" ]; then
    SCRIPT_DIR="$INSTALL_SHARE"
  else
    echo "[ERROR] 找不到 run_recorder.sh"
    echo "请在 install/dexe_recorder/share/dexe_recorder/ 目录下执行，或确保包已部署"
    exit 1
  fi
fi

DEPLOY_DIR="$HOME/workspace/dexe_recorder"

echo "=== 部署 dexe_recorder 到 $DEPLOY_DIR ==="
echo ""

# 创建目录
mkdir -p "$DEPLOY_DIR/config" "$DEPLOY_DIR/data" "$DEPLOY_DIR/hdf5" "$DEPLOY_DIR/log"

# 拷贝脚本和配置
cp "$SCRIPT_DIR/run_recorder.sh" "$DEPLOY_DIR/"
cp "$SCRIPT_DIR/recorderctl.sh" "$DEPLOY_DIR/"
cp "$SCRIPT_DIR/convert_to_hdf5.sh" "$DEPLOY_DIR/"
cp "$SCRIPT_DIR/convert_to_hdf5.py" "$DEPLOY_DIR/"
cp "$SCRIPT_DIR/config/auto_recorder.yaml" "$DEPLOY_DIR/config/"

chmod +x "$DEPLOY_DIR/run_recorder.sh" "$DEPLOY_DIR/recorderctl.sh" "$DEPLOY_DIR/convert_to_hdf5.sh"

# 确保可执行文件有执行权限（tar 解压可能丢失权限）
EXEC_DIR="$HOME/workspace/install/dexe_recorder/lib/dexe_recorder"
if [ -d "$EXEC_DIR" ]; then
  chmod +x "$EXEC_DIR"/* 2>/dev/null || true
fi

echo "部署完成！"
echo ""
echo "目录结构："
ls -la "$DEPLOY_DIR/"
echo ""
echo "使用方法："
echo "  1. 启动节点:  cd ~/workspace/dexe_recorder && ./run_recorder.sh &"
echo "  2. 开始录制:  ./recorderctl.sh start"
echo "  3. 停止录制:  ./recorderctl.sh stop"
echo "  4. 查询状态:  ./recorderctl.sh status"
echo "  5. 转 HDF5:  cd ~/workspace/dexe_recorder && ./convert_to_hdf5.sh"
echo ""
echo "数据位置："
echo "  录制数据: ~/workspace/dexe_recorder/data/"
echo "  HDF5文件: ~/workspace/dexe_recorder/hdf5/"
echo "  日志文件: ~/workspace/dexe_recorder/log/"
echo "  配置文件: ~/workspace/dexe_recorder/config/auto_recorder.yaml"