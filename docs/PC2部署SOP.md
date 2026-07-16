# PC2 部署录制功能 SOP

> 前提：你已经在 PC2 终端上，本地已有一个 `dexe_recorder.tar` 文件（从 install 目录压缩好的，`tar -xvf dexe_recorder.tar` 解压即可得到 `dexe_recorder/` 目录）
> 目标：在 PC2 上部署 dexe_recorder，实现自主导航模式下的数据录制

---

## 前置条件检查

```bash
# 1. 确认 ROS2 Humble 已安装
ls /opt/ros/humble/setup.bash

# 2. 确认 install 环境存在且包含依赖包（end_effector_interfaces 等）
ls ~/workspace/install/setup.bash
ls ~/workspace/install/end_effector_interfaces/

# 3. 确认 GStreamer 硬件插件可用
gst-inspect-1.0 nvv4l2h264enc | head -3
gst-inspect-1.0 nvjpegdec | head -3

# 4. 确认手部相机服务在运行（PC2 本地话题）
ros2 topic list | grep camera_l
ros2 topic list | grep camera_r
```

如果第 2 步失败（全新机器），需要先部署完整的 `~/workspace/install/` 环境（含所有接口包），这不是本 SOP 的范围。

---

## 部署步骤

### 第 1 步：解压压缩包到 install 目录

```bash
# 确认当前目录有 dexe_recorder.tar
ls dexe_recorder.tar

# 解压到 ~/workspace/install/ 下（合并到已有的 install 环境）
tar -xvf dexe_recorder.tar -C ~/workspace/install/

# 验证解压结果
ls ~/workspace/install/dexe_recorder/lib/dexe_recorder/dexe_recorder_node
```

### 第 2 步：修复可执行文件权限

tar 解压可能丢失执行权限，手动修复：

```bash
chmod +x ~/workspace/install/dexe_recorder/lib/dexe_recorder/*
```

### 第 3 步：执行 deploy.sh 部署脚本和配置

```bash
cd ~/workspace/install/dexe_recorder/share/dexe_recorder
chmod +x deploy.sh
./deploy.sh
```

deploy.sh 会自动完成：
- 拷贝 `run_recorder.sh`、`recorderctl.sh`、`convert_to_hdf5.sh`、`convert_to_hdf5.py` 到 `~/workspace/dexe_recorder/`
- 拷贝配置文件到 `~/workspace/dexe_recorder/config/`
- 创建 `data/`、`hdf5/`、`log/` 目录

### 第 4 步：验证部署

```bash
# 确认文件就位
ls ~/workspace/dexe_recorder/
# 应包含：run_recorder.sh  recorderctl.sh  convert_to_hdf5.sh  convert_to_hdf5.py  config/  data/  hdf5/  log/

# 确认可执行文件有权限
ls -la ~/workspace/install/dexe_recorder/lib/dexe_recorder/dexe_recorder_node
# 应为 -rwxr-xr-x（有 x 权限）
```

---

## 启动与测试

### 第 5 步：启动录制节点

```bash
cd ~/workspace/dexe_recorder
./run_recorder.sh &
```

日志输出 `dexe_recorder_node 启动成功` 即正常。日志文件在 `~/workspace/dexe_recorder/log/dexe_recorder.log`。

### 第 6 步：测试录制

```bash
cd ~/workspace/dexe_recorder

# 查询状态（应返回 idle）
./recorderctl.sh status

# 开始录制
./recorderctl.sh start

# 等 10 秒后查询（应返回 recording + frames）
sleep 10
./recorderctl.sh status

# 停止录制
./recorderctl.sh stop
```

### 第 7 步：检查录制数据

```bash
ls ~/workspace/dexe_recorder/data/
# 应有以时间戳命名的 session 目录

# 检查 session 内容
ls ~/workspace/dexe_recorder/data/<session_id>/
# 应有：head/left/video.mp4  head/right/video.mp4  hand/left/video.mp4  hand/right/video.mp4  pose_record_*.json  metadata.jsonl
```

### 第 8 步（可选）：转换为 HDF5

```bash
cd ~/workspace/dexe_recorder
./convert_to_hdf5.sh
# HDF5 文件输出到 ~/workspace/dexe_recorder/hdf5/
```

---

## 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `No executable found` | tar 解压丢失执行权限 | 第 2 步已修复；如仍报错：`chmod +x ~/workspace/install/dexe_recorder/lib/dexe_recorder/*` |
| `cannot open shared object file: libstd_srvs...so` | install 环境缺少依赖包 | 需先部署完整的 `~/workspace/install/`（含接口包） |
| `The passed service type is invalid` | 终端未 source ROS2 环境 | 用 `recorderctl.sh`（自动 source）；手动则需先 `source /opt/ros/humble/setup.bash && source ~/workspace/install/setup.bash` |
| 启动失败，日志无新内容 | 可执行文件路径或权限问题 | 检查 `~/workspace/install/dexe_recorder/lib/dexe_recorder/dexe_recorder_node` 是否存在且有 `x` 权限 |

---

## 更新部署（代码更新后）

当拿到新的 `dexe_recorder.tar` 后，重新部署只需：

```bash
# 1. 停旧进程
pkill -f dexe_recorder_node 2>/dev/null

# 2. 覆盖安装包
rm -rf ~/workspace/install/dexe_recorder
tar -xvf dexe_recorder.tar -C ~/workspace/install/
chmod +x ~/workspace/install/dexe_recorder/lib/dexe_recorder/*

# 3. 重启节点
cd ~/workspace/dexe_recorder
./run_recorder.sh &
```

> 注意：更新只覆盖 `install/dexe_recorder` 包，不影响 `~/workspace/dexe_recorder/` 下的配置和数据。
