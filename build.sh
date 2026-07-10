#!/usr/bin/env bash
set -e  # 出错立即退出

WORKSPACE_DIR=$(pwd)
INSTALL_DIR="${WORKSPACE_DIR}/install"
OUTPUT_DIR="${WORKSPACE_DIR}/release"
TAR_PATH="${OUTPUT_DIR}/install.tar.gz"

source /opt/ros/humble/setup.bash

# === 1. 检查 ROS 环境 ===
if [ -z "$ROS_DISTRO" ]; then
    echo "[ERROR] 请先 source 对应ROS环境，例如："
    echo "   source /opt/ros/humble/setup.bash"
    exit 1
fi
echo "[OK] ROS版本: $ROS_DISTRO"

# === 2. 检查并更新 git 子模块 ===
if [ -f "${WORKSPACE_DIR}/.gitmodules" ]; then
    echo "[INFO] 检测到 .gitmodules，正在同步子模块..."
    git submodule sync --recursive
    git submodule update --init --recursive
    echo "[OK] 子模块已更新完成。"
else
    echo "[INFO] 未检测到 .gitmodules，跳过子模块更新。"
fi

# === 3. 编译 ===
echo "[INFO] 开始 colcon build ..."
colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release

# === 4. 打包 install 目录 ===
mkdir -p "$OUTPUT_DIR"
echo "[INFO] 清理旧的压缩包..."
rm -f "$TAR_PATH"

echo "[INFO] 正在打包 install 目录为 install.tar.gz..."
tar -czf "$TAR_PATH" -C "$WORKSPACE_DIR" install

# === 5. 完成 ===
echo "[OK] 编译完成并打包成功！"
echo "[INFO] 输出路径：$TAR_PATH"
