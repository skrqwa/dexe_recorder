# Spec: dexe_recorder HDF5 转换与遥操对齐 + 反转工具移植

> **状态**：ready-for-agent
> **日期**：2026-07-17
> **来源**：grilling 会话对齐结论

---

## Problem Statement

dexe_recorder 的 `convert_to_hdf5.py` 当前生成的 HDF5 文件与遥操 `cloud_upload/hdf5_converter.py` 生成的格式不一致，导致：
1. 云端训练流水线无法直接消费 dexe_recorder 的 HDF5
2. 反转工具（`hdf5_to_raw.py`）无法还原 dexe_recorder 的 HDF5
3. 关节数据未插值对齐到相机时间戳，帧数不一致
4. 缺少 `metadata_jsonl`/`tactile_jsonl`/`subdirs` 等反转必需字段

同时，dexe_recorder 目前没有 HDF5 反转工具，无法从 HDF5 还原原始录制目录结构。

## Solution

1. **改造 `convert_to_hdf5.py`**：对齐遥操 `hdf5_converter.py` 的 HDF5 输出格式，但不复用遥操代码（适配 dexe_recorder 的实际录制产物结构）
2. **移植反转工具**：从遥操 `scripts/tools/hdf5_to_raw_toolkit/` 移植到 dexe_recorder，包含 `hdf5_to_raw.py`、`image_video_encode.py`（解码部分）、`hdf5_to_raw.sh`

## User Stories

1. 作为数据工程师，我希望 dexe_recorder 生成的 HDF5 能直接被云端训练流水线消费，这样我不需要额外转换
2. 作为数据工程师，我希望 HDF5 的关节数据插值对齐到相机时间戳，这样每帧的关节和图片严格对应
3. 作为数据工程师，我希望 HDF5 包含 `metadata_jsonl` 原始内容，这样反转时能 bytes 级还原
4. 作为数据工程师，我希望 HDF5 包含 `tactile_jsonl`（如有），这样触觉数据不丢失
5. 作为数据工程师，我希望 HDF5 包含 `subdirs` 属性，这样反转时能还原空目录结构
6. 作为数据工程师，我希望 JPEG 模式下严格校验各相机帧数和文件名，这样能尽早发现录制异常
7. 作为数据工程师，我希望 VIDEO 模式下只校验帧数相等，因为 image_path 是虚拟 jpg 名
8. 作为数据工程师，我希望 HDF5 的相机 timestamps 来自 metadata.jsonl，这样和遥操一致
9. 作为数据工程师，我希望 dexe_recorder 有反转工具，这样我能从 HDF5 还原原始目录用于排查
10. 作为数据工程师，我希望反转工具能解码 MP4 为 jpg 图片序列，这样还原后的目录和原始录制结构一致
11. 作为数据工程师，我希望反转工具能还原 `pose_record_*.json`，这样关节数据可读
12. 作为数据工程师，我希望反转工具能还原 `metadata.jsonl` 和 `tactile.jsonl`，这样辅助文件完整
13. 作为数据工程师，我希望反转工具支持批量处理，这样能一次还原多个 HDF5
14. 作为开发者，我希望转换和反转有往返一致性测试，这样能防止回归
15. 作为开发者，我希望关节列表是动态的（不强制 35 个），这样能适配不同末端执行器配置

## Implementation Decisions

### 复用策略
- dexe_recorder 维护独立的 `convert_to_hdf5.py`，不复用遥操 `hdf5_converter.py` 代码
- 理由：录制产物结构差异大（VIDEO 模式已有 mp4、metadata.jsonl 内容、关节数据来源），强行复用需打补丁

### 关节数据源
- HDF5 的 joints 数据来自 `pose_record_*.json`（命令值），与遥操一致
- 关节列表动态化：录到什么写什么，不强制 35 个
- 当 ACT 不发布 EE 命令时，只有手臂关节（20个），这是合理的真实记录

### metadata.jsonl 的 image_path
- VIDEO 模式下 `image_path` 写 `head/left/000000.jpg`（当前行为保持）
- 格式和遥操 JPEG 模式一致，作为"帧标识"
- 转换脚本 VIDEO 模式忽略 image_path，直接读对应目录的 `video.mp4`

### 帧对齐策略
- 以参考相机（head_left 优先，其次任意 _left，最后任意）的时间戳为基准
- 关节数据用 `np.interp` 插值到参考相机时间戳
- HDF5 的 `frames` = 参考相机帧数
- 与遥操 `hdf5_converter.py` 的 `interp_qpos` 逻辑一致

### 相机严格校验
- JPEG 模式：校验各相机帧数相等 + 图片文件名逐一对应（与遥操 `parse_metadata_aligned` 一致）
- VIDEO 模式：只校验各相机帧数相等（image_path 是虚拟 jpg 名，文件名本就一致）
- 校验失败抛 `ValueError`，不静默跳过

### HDF5 缺失字段补齐
- `metadata_jsonl` dataset：读 metadata.jsonl 整体存为 bytes（`np.frombuffer` + `uint8`）
- `tactile_jsonl` dataset：如果存在 tactile.jsonl 则存，不存在跳过
- `subdirs` 属性：扫描录制目录所有子目录相对路径，存为 JSON 字符串
- 跳过 `series_number`、`end_effector_value_range`（需外部配置，按需后补）

### 相机 timestamps 来源
- VIDEO 模式和 JPEG 模式统一：用 metadata.jsonl 的参考相机时间戳
- 转为 uint64 纳秒（`unix_to_uint64_ns`）

### video.mp4 路径推断
- 从 `image_path` 的目录部分 + `video.mp4` 拼接
- 如 `head/left/000000.jpg` -> `head/left/video.mp4`

### 相机组命名
- 用 `f"camera_{cam_type}"` 拼接（如 `camera_head_left`）
- 与遥操默认 `camera_groups` 映射结果一致，不引入配置表

### 反转工具移植
- 从遥操 `scripts/tools/hdf5_to_raw_toolkit/` 移植 3 个文件到 `dexe_recorder/scripts/hdf5_to_raw_toolkit/`
- `image_video_encode.py`：保留 `decode_mp4_bytes_to_rgb_images` 及其依赖（PyAV 解码）
- `hdf5_to_raw.py`：完整移植，包含 `HDF5ToRawConverter`、`AlignmentStrategy`、批量处理、CLI
- `hdf5_to_raw.sh`：完整移植
- 依赖：`h5py`、`numpy`、`Pillow`、`av`（PyAV）

## Testing Decisions

### 测试原则
- 只测外部行为（CLI 输入输出），不测内部函数实现
- 优先端到端测试，不单独测 `np.interp`、`MapEeJointName` 等内部逻辑

### 测试 Seams

**Seam 1：`convert_to_hdf5.py` 端到端**
- 输入：一个录制 session 目录
- 输出：HDF5 文件
- 验证：HDF5 结构（groups/datasets/attrs）、joints 插值正确性、metadata_jsonl/tactile_jsonl/subdirs 完整性

**Seam 2：`hdf5_to_raw.py` 端到端**
- 输入：HDF5 文件
- 输出：还原的目录结构
- 验证：图片数量、pose_record 关节数据、metadata.jsonl bytes 级一致

**Seam 3：往返一致性测试（Round-trip，最高价值）**
- 录制目录 -> HDF5 -> 反转目录
- 验证：pose_record 关节数值一致、metadata.jsonl 内容一致、图片数量一致
- 一个测试覆盖两个工具

### 测试数据
- 使用 PC2 上的真实录制数据（如 `20260716_165208`，含 head_left/head_right + VIDEO 模式 + 1348帧 pose_record）
- 测试前需将样本数据同步到测试机器

### 测试位置
- 新建 `dexe_recorder/tests/` 目录
- 用 pytest，文件命名 `test_convert_to_hdf5.py`、`test_hdf5_to_raw.py`、`test_roundtrip.py`

## Out of Scope

1. **PC2 重新部署最新 30Hz WriterLoop 代码**：当前 PC2 部署的是旧版（100Hz pose_record），需单独部署验证，不在本 spec 范围
2. **`series_number` / `end_effector_value_range` 属性**：需从外部配置读取，暂不实现
3. **feedback_record 的 HDF5 转换**：当前只转换 pose_record，feedback_record 的 HDF5 适配不在范围
4. **遥操 `hdf5_converter.py` 的修改**：只对齐 dexe_recorder 侧，不动遥操代码
5. **HDF5 文档更新**：`HDF5文件格式说明.md` 已描述目标格式，无需修改

## Further Notes

### 已知差异（需后续逐步对齐）
- dexe_recorder 的 pose_record 当前在 PC2 上是 100Hz（旧版部署），重新部署最新代码后为 30Hz
- 30Hz 下 pose_record ~402帧，相机 409帧，插值后以相机 409帧为准

### 参考文件
- 遥操 converter：`dexe_teleoperate/src/backend/cloud_upload/hdf5_converter.py`
- 遥操反转工具：`dexe_teleoperate/scripts/tools/hdf5_to_raw_toolkit/`
- HDF5 格式文档：`dexe_teleoperate/.doc/HDF5文件格式说明.md` v1.3
- dexe_recorder 当前 converter：`dexe_recorder/scripts/convert_to_hdf5.py`

### grilling 会话决策记录
9 个决策点全部对齐，详见会话总结表：

| # | 决策项 | 方案 |
|---|--------|------|
| 1 | 复用策略 | B：独立脚本，输出格式对齐 |
| 2 | 关节数据源 | A：pose_record（命令值），关节列表动态 |
| 3 | metadata image_path | A：VIDEO 模式写 000000.jpg |
| 4 | 帧对齐策略 | A：相机帧为基准，np.interp 插值 |
| 5 | 相机校验 | A：JPEG 严格，VIDEO 只校验帧数 |
| 6 | 缺失字段补齐 | A：补 metadata_jsonl/tactile_jsonl/subdirs |
| 7 | 相机 timestamps | A：用 metadata 时间戳 |
| 8 | video.mp4 路径 | A：image_path 目录 + video.mp4 |
| 9 | 相机组命名 | A：f"camera_{cam_type}" 拼接 |
