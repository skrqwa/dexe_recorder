# dexe_recorder

Auto 模式 HDF5 数据采集录制节点（C++ / ROS 2 Humble）。

## 功能

- auto 模式下采集关节指令/反馈（100Hz）、相机图像、末端执行器
- start/stop ROS2 服务接口，对接 application
- 录制数据保存为视频（H.264）或图片（JPEG），优先视频
- 手动触发转换为 HDF5（支持视频/图片两种格式）
- 话题可配置，默认与遥操录制一致

## 编译

```bash
./build.sh
```

## 运行

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=20

ros2 run dexe_recorder dexe_recorder_node --ros-args -p config:=config/auto_recorder.yaml
```

## 服务接口

```bash
ros2 service call /dexe_recorder/start_recording std_srvs/srv/Trigger
ros2 service call /dexe_recorder/stop_recording std_srvs/srv/Trigger
ros2 service call /dexe_recorder/get_status std_srvs/srv/Trigger
```

## 配置

见 `config/auto_recorder.yaml`：数据源话题、存储路径、缓冲大小、分段时长。

## 集成

auto_mode.sh 增加一行：
```bash
taskset -c 1 ros2 run dexe_recorder dexe_recorder_node > ${DEPLOY_DIR}/../log/auto/dexe_recorder.log 2>&1 &
```
