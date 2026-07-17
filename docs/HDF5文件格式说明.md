# 端侧 HDF5 文件格式说明

> **版本**：v1.4
> **日期**：2026-07-17
> **用途**：端侧录制数据压缩为 HDF5（视频编码），用于云端传输；云端可反转为原始数据结构

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.3 | 2026-07-16 | 初始版本（遥操 cloud_upload/hdf5_converter.py） |
| v1.4 | 2026-07-17 | 补充 dexe_recorder 适配说明（VIDEO 模式 image_path 虚拟 jpg 名、关节列表动态化、可选属性） |

---

## 一、文件概览

| 项目 | 说明 |
|------|------|
| 文件格式 | HDF5 (h5py) |
| 压缩方式 | 相机图像用 H.264 (GStreamer nvv4l2h264enc) 编码为 MP4，关节用 gzip |
| 典型大小 | 10-40 MB（344-582 帧，2-4 路相机） |
| 压缩率 | 约 89%（原始 456M → HDF5 50M） |

---

## 二、顶层结构

```
/                                    # root
├── camera_head_left/                # 相机组（每个相机一个 group）
├── camera_head_right/
├── camera_hand_left/                # 可选，取决于录制时是否有 hand 相机
├── camera_hand_right/
├── joints/                          # 关节数据组
├── metadata_jsonl                   # 原始 metadata.jsonl 整体存储（bytes）
└── tactile_jsonl                    # 原始 tactile.jsonl 整体存储（bytes，可能为空）
```

---

## 三、Root 属性（attrs）

| 属性 | 类型 | 说明 |
|------|------|------|
| `frames` | int | 总帧数（所有相机和关节数据对齐后的帧数） |
| `cameras` | str (JSON) | 相机列表，如 `["head_left", "head_right"]` |
| `language` | str | 语言提示（可为空） |
| `robot_type` | str | 机器人型号，如 `W1_Pro` |
| `sample_rate` | float | 采样率（Hz），如 `30.0` |
| `series_number` | str | 设备序列号 |
| `description` | str | 文件描述 |
| `alignment_method` | str | 对齐方式，如 `nearest_timestamp` |
| `subdirs` | str (JSON) | 原始 session 目录下所有子目录路径列表，用于反转时创建空目录 |

---

## 四、相机组（camera_*）

每个相机一个 group，命名规则：`camera_<位置>_<方向>`，如 `camera_head_left`。

### Group 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `type` | str | 固定 `image` |
| `encoding` | str | 编码方式：`mp4`（视频模式）或 `jpeg`（逐帧模式） |
| `width` | int | 图像宽度（像素），如 `1920` |
| `height` | int | 图像高度（像素），如 `1080` |
| `channels` | int | 通道数，固定 `3`（RGB） |
| `frames` | int | 帧数（与 root frames 一致） |
| `sample_rate` | float | 采样率（Hz） |

### Datasets

| Dataset | Shape | Dtype | 说明 |
|---------|-------|-------|------|
| `data` | `()` | object (vlen uint8) | MP4 模式：单个 varlen blob，内含完整 MP4 视频流；JPEG 模式：shape=(N,)，每帧一个 JPEG blob |
| `timestamps` | `(N,)` | uint64 | 每帧时间戳（Unix 纳秒），N = frames |

---

## 五、关节组（joints）

### Group 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `type` | str | 固定 `vector` |
| `frames` | int | 帧数（与 root frames 一致） |
| `sample_rate` | float | 采样率（Hz） |
| `columns` | str (JSON) | 关节名列表，如 `["ANKLE", "KNEE", ...]`，共 35 个 |
| `end_effector_value_range` | str (JSON) | 末端执行器值范围，如 `[0.0, 100.0]` |

### Datasets

| Dataset | Shape | Dtype | 说明 |
|---------|-------|-------|------|
| `data` | `(N, D)` | float32 | 关节数据，N = frames，D = 关节数（通常 35） |
| `timestamps` | `(N,)` | uint64 | 每帧时间戳（Unix 纳秒） |

### 关节数据说明

- 关节数据已通过 `np.interp` 插值到相机帧的时间戳（与算法版本一致）
- 不做末端缩放（DEXTROUSHAND/GRIPPER），保持原始值
- 关节列表（35 个）：

```
ANKLE, KNEE, BUTTOCK, WAIST, NECK1, NECK2,
LEFT_J1~J7, RIGHT_J1~J7,
LEFT_HAND_THUMB1/2, LEFT_HAND_INDEX/MIDDLE/RING/PINKY,
RIGHT_HAND_THUMB1/2, RIGHT_HAND_INDEX/MIDDLE/RING/PINKY,
LEFT_GRIPPER, RIGHT_GRIPPER, joy_y_lift
```

---

## 六、原始文件存储

| Dataset | Shape | Dtype | 说明 |
|---------|-------|-------|------|
| `metadata_jsonl` | `(N,)` | uint8 | 原始 `metadata.jsonl` 文件的完整内容（bytes） |
| `tactile_jsonl` | `(N,)` | uint8 | 原始 `tactile.jsonl` 文件的完整内容（bytes，可能为空，N=0） |

反转时直接将 bytes 写回文件，内容与原始完全一致。

---

## 七、对齐方式

与算法版本（`w1_telecontrol_to_hdf5_xx.py`）一致：

1. **相机对齐**：各相机帧数必须相等，图片名逐一对应（严格校验，不对齐则报错）
2. **参考相机**：优先 `head_left`
3. **关节数据**：用 `np.interp` 插值到参考相机的时间戳
4. **不丢帧**：所有相机已严格对齐，不丢弃任何帧
5. **不缩放**：关节数据保持原始值，不做末端缩放

---

## 八、HDF5 文件路径规则

HDF5 文件存放在 `data/hdf5/` 目录，结构镜像 `data/recorded/`：

| 原始 session 目录 | HDF5 文件路径 |
|-------------------|---------------|
| `data/recorded/test2/20260617_122549/` | `data/hdf5/test2/20260617_122549.hdf5` |
| `data/recorded/20260630_152358/20260617_121107/` | `data/hdf5/20260630_152358/20260617_121107.hdf5` |

---

## 九、反转工具

位置：`scripts/tools/hdf5_to_raw_toolkit/`

```
hdf5_to_raw_toolkit/
├── hdf5_to_raw.py        # 反转主程序
├── hdf5_to_raw.sh        # 执行脚本
└── image_video_encode.py # 视频解码库
```

### 用法

```bash
# 安装依赖
pip install h5py numpy Pillow av

# 单文件反转
./hdf5_to_raw.sh data.hdf5 output/

# 批量反转
./hdf5_to_raw.sh hdf5_dir/ output/
```

### 反转输出结构

```
output/<session_name>/
├── head/left/000000.jpg ... 000NNN.jpg    # 从 MP4 解码的图片
├── head/right/000000.jpg ...
├── hand/left/000000.jpg ...               # 如有 hand 相机
├── hand/right/000000.jpg ...
├── metadata.jsonl                         # 从 HDF5 还原的原始文件
├── tactile.jsonl                          # 从 HDF5 还原的原始文件
└── pose_record_<session>.json             # 从 joints 数据重建
```

### 注意事项

- 反转的图片经过 MP4 有损压缩，与原始图片视觉一致但文件大小不同
- 图片分辨率与原始完全一致
- `metadata.jsonl` 和 `tactile.jsonl` 与原始文件内容完全一致（bytes 级别）
- `pose_record` 的关节数值为插值后的值（与算法版本一致）
- 空目录（如无 hand 相机时的 `hand/left`）也会创建
- 不依赖 GPU，纯 CPU 软解码，可部署到任何机器

---

## 十、dexe_recorder 适配说明（v1.4 新增）

dexe_recorder 的 `scripts/convert_to_hdf5.py` 生成的 HDF5 与本格式说明完全兼容，以下为适配差异：

### 录制产物差异

| 项 | 遥操 | dexe_recorder |
|---|---|---|
| VIDEO 模式 | jpg 序列 -> GStreamer 编码 -> mp4 | 录制时直接产出 mp4 |
| metadata.jsonl 的 image_path | 真实 jpg 路径 | VIDEO 模式写虚拟 jpg 名（如 `head/left/000000.jpg`），实际文件是 `video.mp4` |
| pose_record 帧率 | 30Hz | 30Hz（最新版 WriterLoop；旧版 100Hz 需重新部署） |

### 转换适配

1. **VIDEO 模式 image_path**：image_path 写的是 `000000.jpg`，转换脚本 VIDEO 模式忽略 image_path，直接读对应目录的 `video.mp4`
2. **video.mp4 路径推断**：`image_path` 的目录部分 + `video.mp4`（如 `head/left/000000.jpg` -> `head/left/video.mp4`）
3. **关节列表动态化**：不强制 35 个关节，按 `pose_record_*.json` 实际录到的关节为准（ACT 不发布 EE 命令时只有 20 个手臂关节）
4. **帧对齐**：以参考相机（head_left 优先）的时间戳为基准，关节数据用 `np.interp` 插值

### 可选属性（不强制）

以下属性 dexe_recorder 当前不写入，按需后补：

| 属性 | 说明 | 来源 |
|------|------|------|
| `series_number` | 设备序列号 | 需从外部配置读取 |
| `end_effector_value_range` | 末端执行器值范围 | 需从 teleop_config.yaml 读取 |

### 转换工具

| 工具 | 位置 | 说明 |
|------|------|------|
| 转换 | `dexe_recorder/scripts/convert_to_hdf5.py` | 录制目录 -> HDF5 |
| 反转 | `dexe_recorder/scripts/hdf5_to_raw_toolkit/` | HDF5 -> 原始目录（从遥操移植） |
