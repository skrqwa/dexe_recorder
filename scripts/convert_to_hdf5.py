#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
# -*- coding: utf-8 -*-
"""
dexe_recorder 录制数据转 HDF5

直接读取 dexe_recorder 的录制产物（VIDEO 模式的 mp4 或 JPEG 模式的 jpg），
写入与遥操兼容的 HDF5 格式。不依赖 GStreamer 编码器。

用法:
  python3 convert_to_hdf5.py --input <recorded_dir> --output <hdf5_dir> [--format video|jpeg]

示例:
  python3 convert_to_hdf5.py --input /home/dexforce/data/recorded_auto --output /home/dexforce/data/hdf5
  python3 convert_to_hdf5.py --input /home/dexforce/data/recorded_auto --output /tmp/hdf5 --format jpeg

依赖:
  h5py, numpy, PyAV（VIDEO 探测/对齐）,
  PyGObject + GStreamer（Jetson VIDEO 对齐/修复）,
  Pillow（JPEG 模式需要读图片尺寸）, tqdm（可选，进度条）

HDF5 结构（与遥操 w1_telecontrol_to_hdf5_airs.py 兼容）:
  session.hdf5
  ├── (attrs) frames, cameras, sample_rate, robot_type, ...
  ├── camera_head_left/
  │   ├── (attrs) encoding="mp4"或"jpeg", width, height, frames
  │   ├── data         # video: shape=() varlen blob; jpeg: shape=(N,) varlen 数组
  │   └── timestamps   # shape=(N,), uint64 纳秒
  ├── camera_head_right/  (同上)
  └── joints/
      ├── (attrs) columns, frames
      ├── data         # shape=(N, num_joints), float32
      └── timestamps   # shape=(N,), uint64 纳秒
"""

import os
import sys
import json
import argparse
import tempfile
import numpy as np
from pathlib import Path
from typing import List, Dict, Optional, Tuple

REQUIRED_METADATA_STRING_FIELDS = (
    "system_version",
    "hardware_version",
    "ee_type",
    "ee_name",
)

DEFAULT_MEMORY_PEAK_FACTOR = 4.0
DEFAULT_MEMORY_RESERVE_BYTES = 512 * 1024 * 1024
DEFAULT_WARN_VIDEO_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_VIDEO_BYTES = 2048 * 1024 * 1024

try:
    import h5py
except ImportError:
    print("[ERROR] 需要 h5py: pip install h5py")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


def load_metadata(data_dir: Path) -> List[Dict]:
    """读取 metadata.jsonl，返回每行一个 dict"""
    meta_path = data_dir / "metadata.jsonl"
    if not meta_path.exists():
        return []
    entries = []
    with open(meta_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def load_pose_record(data_dir: Path) -> Optional[Dict]:
    """读取 pose_record_*.json"""
    json_files = list(data_dir.glob("pose_record_*.json"))
    if not json_files:
        return None
    with open(json_files[0], "r", encoding="utf-8") as f:
        return json.load(f)


def load_feedback_record(data_dir: Path) -> Optional[Dict]:
    """读取可选的 feedback_record_*.json。"""
    json_files = list(data_dir.glob("feedback_record_*.json"))
    if not json_files:
        return None
    with open(json_files[0], "r", encoding="utf-8") as f:
        return json.load(f)


def build_airs_root_attrs(pose_record: Dict) -> Dict:
    """从 pose_record 构造 AIRS 根属性，缺失字段只用契约空值。"""
    metadata = pose_record.get("metadata", {})
    if not isinstance(metadata, dict):
        print("  [WARN] METADATA_INVALID_TYPE expected=dict fallback=empty")
        metadata = {}

    attrs = {
        "robot_type": str(pose_record.get("robot_type", metadata.get("robot_type", "W1_Pro"))),
        "series_number": str(
            pose_record.get("series_number", metadata.get("series_number", "")) or ""),
        "language": str(
            pose_record.get(
                "language_prompt",
                pose_record.get("language", metadata.get("language", "")),
            )
            or ""),
    }
    for field in REQUIRED_METADATA_STRING_FIELDS:
        value = metadata.get(field, "")
        attrs[field] = str(value or "")
        if not attrs[field]:
            print(f"  [WARN] METADATA_REQUIRED_FIELD_MISSING field={field}")

    ee_value_range = metadata.get("ee_value_range", [])
    if not isinstance(ee_value_range, list):
        print("  [WARN] METADATA_INVALID_FIELD field=ee_value_range expected=list fallback=[]")
        ee_value_range = []
    if not ee_value_range:
        print("  [WARN] METADATA_REQUIRED_FIELD_MISSING field=ee_value_range")

    intervention_segments = metadata.get("intervention_segments", [])
    if not isinstance(intervention_segments, list):
        print(
            "  [WARN] METADATA_INVALID_FIELD "
            "field=intervention_segments expected=list fallback=[]")
        intervention_segments = []

    attrs["ee_value_range"] = json.dumps(ee_value_range, ensure_ascii=False)
    attrs["intervention_segments"] = json.dumps(
        intervention_segments, ensure_ascii=False)
    return attrs


def group_metadata_by_camera(metadata: List[Dict]) -> Dict[str, List[Dict]]:
    """按 camera_type 分组，按 frame_id 排序"""
    cameras = {}
    for entry in metadata:
        cam = entry.get("camera_type", "")
        if cam not in cameras:
            cameras[cam] = []
        cameras[cam].append(entry)
    for cam in cameras:
        cameras[cam].sort(key=lambda x: x.get("frame_id", 0))
    return cameras


def get_image_shape(data_dir: Path, image_path: str) -> Optional[Tuple[int, int, int]]:
    """读取一张图片获取 shape (H, W, C)"""
    try:
        from PIL import Image
        full_path = data_dir / image_path
        if full_path.exists():
            img = Image.open(full_path)
            return (img.height, img.width, 3)
    except Exception:
        pass
    return None


def unix_to_uint64_ns(ts: float) -> int:
    """Unix 时间戳（秒）转纳秒 uint64"""
    return int(ts * 1e9)


def find_valid_sessions(input_dir: Path) -> List[Path]:
    """查找包含 metadata.jsonl + pose_record_*.json 的 session 目录"""
    sessions = []
    if (input_dir / "metadata.jsonl").exists():
        # 输入本身就是一个 session 目录
        sessions.append(input_dir)
    else:
        for item in sorted(input_dir.iterdir()):
            if item.is_dir() and (item / "metadata.jsonl").exists():
                pose_files = list(item.glob("pose_record_*.json"))
                if pose_files:
                    sessions.append(item)
    return sessions


def detect_format(data_dir: Path, camera_groups: Dict) -> str:
    """自动检测录制格式：有 mp4 文件就是 video，否则 jpeg"""
    for cam, entries in camera_groups.items():
        if entries:
            image_path = entries[0].get("image_path", "")
            # 先检查实际文件
            full_path = data_dir / image_path
            if full_path.exists():
                if image_path.endswith(".mp4") or full_path.suffix == ".mp4":
                    return "video"
                if image_path.endswith(".jpg") or full_path.suffix == ".jpg":
                    return "jpeg"
            # 检查 video.mp4 是否存在
            cam_dir = Path(image_path).parent
            video_path = data_dir / cam_dir / "video.mp4"
            if video_path.exists():
                return "video"
            # 检查 jpg 文件
            jpg_path = data_dir / cam_dir / "000000.jpg"
            if jpg_path.exists():
                return "jpeg"
    return "video"  # 默认


def choose_reference_camera(camera_groups: Dict[str, List[Dict]]) -> str:
    """选择参考相机：优先 head_left，其次任意 _left，最后任意。

    与遥操 _choose_reference_camera 策略一致。
    """
    preferred = "head_left"
    if preferred in camera_groups:
        return preferred
    for cam in sorted(camera_groups.keys()):
        if cam.endswith("_left"):
            return cam
    return next(iter(camera_groups.keys()))


def validate_cameras(
    camera_groups: Dict[str, List[Dict]],
    ref_camera: str,
    fmt: str,
) -> None:
    """校验相机数据一致性。

    JPEG 模式：校验各相机帧数相等 + 图片文件名逐一对应。
    VIDEO 模式：只校验各相机帧数相等（image_path 是虚拟 jpg 名）。
    与遥操 parse_metadata_aligned 校验逻辑一致。
    """
    ref_entries = camera_groups[ref_camera]
    ref_count = len(ref_entries)

    if fmt == "jpeg":
        for cam, entries in camera_groups.items():
            if len(entries) != ref_count:
                raise ValueError(
                    f"相机帧数不一致: {cam}={len(entries)}, {ref_camera}={ref_count}")

    if fmt == "jpeg":
        ref_names = [Path(e.get("image_path", "")).name for e in ref_entries]
        for cam, entries in camera_groups.items():
            if cam == ref_camera:
                continue
            mismatches = []
            for i, (ref_name, entry) in enumerate(zip(ref_names, entries)):
                pic_name = Path(entry.get("image_path", "")).name
                if pic_name != ref_name:
                    mismatches.append(
                        f"frame {i}: {ref_camera}={ref_name}, {cam}={pic_name}")
                    if len(mismatches) >= 5:
                        break
            if mismatches:
                raise ValueError(
                    f"图片名不一致: {cam} vs {ref_camera}; {mismatches}")


def entry_timestamp(entry: Dict) -> float:
    """优先返回相机源 ROS 时间戳，旧数据缺失时回退到接收时间。"""
    ros_timestamp = float(entry.get("ros_timestamp", 0.0) or 0.0)
    return ros_timestamp if ros_timestamp > 0.0 else float(entry.get("timestamp", 0.0))


def build_alignment_plan(
    camera: str,
    entries: List[Dict],
    reference_timestamps: np.ndarray,
    max_skew_sec: float = 1.0 / 30.0,
    max_same_source_steps: int = 2,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """为一路相机生成有界最近邻映射、重复标记和源时间戳。"""
    source_timestamps = np.asarray([entry_timestamp(entry) for entry in entries], dtype=np.float64)
    if source_timestamps.size == 0 or np.any(np.diff(source_timestamps) < 0):
        raise ValueError(f"ALIGNMENT_INVALID_TIMESTAMPS camera={camera}")

    right = np.searchsorted(source_timestamps, reference_timestamps, side="left")
    right = np.clip(right, 0, len(source_timestamps) - 1)
    left = np.clip(right - 1, 0, len(source_timestamps) - 1)
    choose_left = np.abs(reference_timestamps - source_timestamps[left]) <= \
        np.abs(source_timestamps[right] - reference_timestamps)
    source_indices = np.where(choose_left, left, right).astype(np.int64)
    skew = np.abs(source_timestamps[source_indices] - reference_timestamps)

    run_length = 1
    duplicate = np.zeros(len(source_indices), dtype=np.uint8)
    for index in range(len(source_indices)):
        if skew[index] > max_skew_sec + 1e-9:
            raise ValueError(
                "ALIGNMENT_SKEW_EXCEEDED "
                f"camera={camera} reference_index={index} source_index={source_indices[index]} "
                f"reference_timestamp={reference_timestamps[index]:.9f} "
                f"source_timestamp={source_timestamps[source_indices[index]]:.9f} "
                f"skew_ms={skew[index] * 1000.0:.3f} max_skew_ms={max_skew_sec * 1000.0:.3f}")
        if index > 0 and source_indices[index] == source_indices[index - 1]:
            duplicate[index] = 1
            run_length += 1
            if run_length > max_same_source_steps:
                raise ValueError(
                    "ALIGNMENT_DUPLICATE_RUN_EXCEEDED "
                    f"camera={camera} reference_index={index} source_index={source_indices[index]} "
                    f"run_length={run_length} max_run_length={max_same_source_steps}")
        else:
            run_length = 1
    return source_indices, duplicate, source_timestamps


def interp_record_frames(
    frames: List[Dict],
    target_ts: np.ndarray,
) -> Tuple[np.ndarray, List[str]]:
    """按每个真实存在的字段独立插值记录帧，不制造缺失字段。"""
    columns = []
    seen = set()
    for frame in frames:
        for key, value in frame.get("data", {}).items():
            if key not in seen and isinstance(value, (int, float)):
                seen.add(key)
                columns.append(key)

    output = np.zeros((len(target_ts), len(columns)), dtype=np.float32)
    for column_index, key in enumerate(columns):
        samples = [
            (float(frame.get("timestamp", 0.0)), float(frame["data"][key]))
            for frame in frames
            if isinstance(frame.get("data", {}).get(key), (int, float))
        ]
        if samples:
            sample_ts = np.asarray([sample[0] for sample in samples], dtype=np.float64)
            sample_values = np.asarray([sample[1] for sample in samples], dtype=np.float32)
            output[:, column_index] = np.interp(target_ts, sample_ts, sample_values)
    return output, columns


def validate_camera_files(data_dir: Path, camera_groups: Dict[str, List[Dict]], fmt: str) -> None:
    """转换前验证所有被声明为已录相机流的媒体文件存在且非空。"""
    for camera, entries in camera_groups.items():
        if not entries:
            continue
        if fmt == "video":
            camera_dir = Path(entries[0].get("image_path", "")).parent
            paths = [data_dir / camera_dir / "video.mp4"]
        else:
            paths = [data_dir / entry.get("image_path", "") for entry in entries]
        for path in paths:
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"媒体文件不存在或为空: camera={camera}, path={path}")


def validate_hdf5_structure(hdf5_path: Path, cameras: List[str], num_frames: int) -> None:
    """原子改名前验证 AIRS 关键结构和行数。"""
    with h5py.File(hdf5_path, "r") as h5:
        if int(h5.attrs.get("frames", -1)) != num_frames:
            raise ValueError(f"HDF5 frames 校验失败: {hdf5_path}")
        if h5["joints/data"].shape[0] != num_frames:
            raise ValueError(f"HDF5 joints 行数校验失败: {hdf5_path}")
        for camera in cameras:
            group = h5[f"camera_{camera}"]
            if group["timestamps"].shape != (num_frames,):
                raise ValueError(f"HDF5 相机时间戳校验失败: camera={camera}")
            if group["data"].shape not in [(), (num_frames,)]:
                raise ValueError(f"HDF5 相机数据结构校验失败: camera={camera}")
            if str(group.attrs.get("encoding", "")) == "mp4":
                if int(group.attrs.get("byte_length", 0)) <= 0:
                    raise ValueError(f"HDF5 MP4 字节长度校验失败: camera={camera}")
                for dataset_name in ("alignment_source_indices", "alignment_duplicate"):
                    if group[dataset_name].shape != (num_frames,):
                        raise ValueError(
                            f"HDF5 对齐数据校验失败: camera={camera}, dataset={dataset_name}")
                original_frames = int(group.attrs.get("alignment_original_frames", 0))
                if group["source_timestamps"].shape != (original_frames,):
                    raise ValueError(
                        f"HDF5 源时间戳校验失败: camera={camera}, original_frames={original_frames}")


def is_valid_existing_hdf5(hdf5_path: Path) -> bool:
    """判断既有文件是否具备可安全跳过的完整 AIRS 关键结构。"""
    try:
        with h5py.File(hdf5_path, "r") as h5:
            frames = int(h5.attrs.get("frames", 0))
            cameras = json.loads(h5.attrs.get("cameras", "[]"))
            if frames <= 0 or not cameras or "joints" not in h5:
                return False
            if h5["joints/data"].shape[0] != frames:
                return False
            for camera in cameras:
                group_name = f"camera_{camera}"
                if group_name not in h5:
                    return False
                group = h5[group_name]
                if "data" not in group or "timestamps" not in group:
                    return False
                if group["timestamps"].shape != (frames,):
                    return False
                encoding = str(group.attrs.get("encoding", ""))
                if encoding == "mp4":
                    if group["data"].shape != () or group["data"].id.get_storage_size() <= 0:
                        return False
                    alignment_datasets = {
                        "alignment_source_indices",
                        "alignment_duplicate",
                        "source_timestamps",
                    }
                    if alignment_datasets.intersection(group.keys()):
                        if not alignment_datasets.issubset(group.keys()):
                            return False
                        for dataset_name in ("alignment_source_indices", "alignment_duplicate"):
                            if group[dataset_name].shape != (frames,):
                                return False
                        original_frames = int(group.attrs.get("alignment_original_frames", 0))
                        if group["source_timestamps"].shape != (original_frames,):
                            return False
                if encoding == "jpeg" and group["data"].shape != (frames,):
                    return False
            return True
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


def estimate_video_duration(entries: List[Dict]) -> float:
    """按源时间戳估算视频覆盖时长，并包含最后一帧的典型周期。"""
    timestamps = np.asarray([entry_timestamp(entry) for entry in entries], dtype=np.float64)
    if len(timestamps) < 2:
        return 0.0
    positive_intervals = np.diff(timestamps)
    positive_intervals = positive_intervals[positive_intervals > 0]
    tail_duration = float(np.median(positive_intervals)) if positive_intervals.size else 0.0
    return max(0.0, float(timestamps[-1] - timestamps[0]) + tail_duration)


def video_compliance_issues(video_info) -> List[str]:
    """返回不符合当前 AIRS H.264 输入契约的编码属性。"""
    issues = []
    if video_info.codec.lower() != "h264":
        issues.append(f"codec={video_info.codec}")
    if "baseline" not in video_info.profile.lower():
        issues.append(f"profile={video_info.profile or 'unknown'}")
    if video_info.pixel_format != "yuv420p":
        issues.append(f"pixel_format={video_info.pixel_format or 'unknown'}")
    if video_info.has_b_frames:
        issues.append("b_frames=true")
    if video_info.max_gop <= 0 or video_info.max_gop > 30:
        issues.append(f"max_gop={video_info.max_gop}")
    return issues


def alignment_statistics(
    entries: List[Dict],
    reference_timestamps: np.ndarray,
    source_indices: np.ndarray,
    duplicate: np.ndarray,
    source_timestamps: np.ndarray,
) -> Dict[str, float]:
    """汇总一路相机的对齐、重复、丢弃和最大偏差指标。"""
    unique_sources = np.unique(source_indices)
    skew = np.abs(source_timestamps[source_indices] - reference_timestamps)
    return {
        "original_frames": len(entries),
        "aligned_frames": len(source_indices),
        "duplicate_frames": int(np.count_nonzero(duplicate)),
        "dropped_source_frames": max(0, len(entries) - len(unique_sources)),
        "max_skew_ms": float(np.max(skew) * 1000.0) if skew.size else 0.0,
    }


def get_mem_available_bytes() -> Optional[int]:
    """读取 Linux MemAvailable；非 Linux 或读取失败时返回 None。"""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def check_video_memory_limits(
    camera: str,
    video_path: Path,
    warn_video_bytes: int,
    max_video_bytes: int,
    memory_peak_factor: float,
    memory_reserve_bytes: int,
) -> None:
    """在写标量变长 blob 前检查固定大小和动态可用内存门槛。"""
    video_bytes = video_path.stat().st_size
    if warn_video_bytes > 0 and video_bytes > warn_video_bytes:
        print(
            f"  [VIDEO_SIZE_WARNING] camera={camera} path={video_path} bytes={video_bytes} "
            f"warning_bytes={warn_video_bytes}")
    if max_video_bytes > 0 and video_bytes > max_video_bytes:
        raise ValueError(
            f"VIDEO_SIZE_LIMIT_EXCEEDED camera={camera} path={video_path} bytes={video_bytes} "
            f"limit_bytes={max_video_bytes} suggestion=检查实时录制码率；异常历史视频请显式使用"
            "--repair-video，超长正常 session 请评估 AIRS 分段契约后调整 --max-video-mib")

    mem_available = get_mem_available_bytes()
    if memory_peak_factor > 0.0 and mem_available is not None:
        estimated_peak = int(video_bytes * memory_peak_factor)
        if estimated_peak + memory_reserve_bytes > mem_available:
            raise ValueError(
                f"VIDEO_MEMORY_LIMIT_EXCEEDED camera={camera} path={video_path} bytes={video_bytes} "
                f"mem_available={mem_available} estimated_peak={estimated_peak} "
                f"reserve_bytes={memory_reserve_bytes} peak_factor={memory_peak_factor:.3f} "
                "suggestion=释放内存或在内存更大的机器转换；不要直接关闭门槛后重试")


def process_session(
    data_dir: Path,
    output_dir: Path,
    fmt: str,
    repair_video: bool = False,
    repair_dir: Optional[Path] = None,
    warn_video_bytes: int = DEFAULT_WARN_VIDEO_BYTES,
    max_video_bytes: int = DEFAULT_MAX_VIDEO_BYTES,
    memory_peak_factor: float = DEFAULT_MEMORY_PEAK_FACTOR,
    memory_reserve_bytes: int = DEFAULT_MEMORY_RESERVE_BYTES,
) -> bool:
    """处理单个 session，生成 HDF5。

    核心对齐逻辑：
    - 以参考相机（head_left 优先）的时间戳为基准
    - 关节数据用 np.interp 插值到参考相机时间戳
    - JPEG 模式严格校验，VIDEO 模式只校验帧数相等
    - 相机 timestamps 用参考相机时间戳，所有相机共用
    """
    session_id = data_dir.name
    output_dir.mkdir(parents=True, exist_ok=True)

    # 加载数据
    metadata = load_metadata(data_dir)
    if not metadata:
        print(f"  [SKIP] {session_id}: 无 metadata.jsonl")
        return False

    pose_record = load_pose_record(data_dir)
    if not pose_record:
        print(f"  [SKIP] {session_id}: 无 pose_record_*.json")
        return False
    airs_root_attrs = build_airs_root_attrs(pose_record)

    # 按相机分组
    camera_groups = group_metadata_by_camera(metadata)
    if not camera_groups:
        print(f"  [SKIP] {session_id}: 无相机数据")
        return False

    # 自动检测格式（如果未指定）
    if fmt == "auto":
        fmt = detect_format(data_dir, camera_groups)
        print(f"  检测到格式: {fmt}")

    # 选择参考相机
    ref_camera = choose_reference_camera(camera_groups)
    print(f"  参考相机: {ref_camera}")

    # 校验相机数据
    validate_cameras(camera_groups, ref_camera, fmt)
    validate_camera_files(data_dir, camera_groups, fmt)

    # 参考相机时间戳（作为 HDF5 的基准时间轴）
    ref_entries = camera_groups[ref_camera]
    ref_timestamps = np.array(
        [entry_timestamp(entry) for entry in ref_entries], dtype=np.float64)
    num_frames = len(ref_entries)
    print(f"  帧数: {num_frames} (参考相机 {ref_camera})")

    # 加载关节数据
    pose_frames = pose_record.get("frames", [])
    if not pose_frames:
        print(f"  [SKIP] {session_id}: 无关节数据")
        return False

    # 每个真实存在的动作字段独立插值，不为缺失字段填零
    print(f"  插值关节数据: {len(pose_frames)} -> {num_frames} 帧")
    qpos_interp, joint_keys = interp_record_frames(pose_frames, ref_timestamps)
    print(f"  关节数: {len(joint_keys)}")

    feedback_record = load_feedback_record(data_dir)
    feedback_interp = None
    feedback_keys = []
    if feedback_record:
        feedback_frames = feedback_record.get("frames", [])
        if feedback_frames:
            feedback_interp, feedback_keys = interp_record_frames(feedback_frames, ref_timestamps)
            print(f"  插值反馈数据: {len(feedback_frames)} -> {num_frames} 帧")

    # 相机列表
    selected_cameras = sorted(camera_groups.keys())

    alignment_plans = {}
    alignment_stats = {}
    video_paths = {}
    video_infos = {}
    alignment_temp = tempfile.TemporaryDirectory(prefix=f".{session_id}.alignment-", dir=output_dir)
    try:
        if fmt == "video":
            try:
                from video_pipeline import align_video, probe_video
            except ImportError as error:
                raise RuntimeError(
                    "VIDEO_DEPENDENCY_MISSING install=PyAV error=" + str(error)) from error
            alignment_failures = []
            for camera in selected_cameras:
                entries = camera_groups[camera]
                try:
                    source_indices, duplicate, source_timestamps = build_alignment_plan(
                        camera, entries, ref_timestamps)
                except ValueError as error:
                    alignment_failures.append(str(error))
                    continue
                alignment_plans[camera] = (source_indices, duplicate, source_timestamps)
                alignment_stats[camera] = alignment_statistics(
                    entries, ref_timestamps, source_indices, duplicate, source_timestamps)
            if alignment_failures:
                raise ValueError(
                    f"ALIGNMENT_FAILED session={session_id} failure_count={len(alignment_failures)} "
                    f"failures={' | '.join(alignment_failures)}")

            for camera in selected_cameras:
                entries = camera_groups[camera]
                source_indices, duplicate, source_timestamps = alignment_plans[camera]
                stats = alignment_stats[camera]
                camera_dir = Path(entries[0].get("image_path", "")).parent
                source_video = data_dir / camera_dir / "video.mp4"
                duration_sec = estimate_video_duration(entries)
                average_bitrate = (
                    source_video.stat().st_size * 8.0 / duration_sec if duration_sec > 0.0 else 0.0)
                abnormal_bitrate = average_bitrate > 8_000_000
                if abnormal_bitrate and not repair_video:
                    raise ValueError(
                        "VIDEO_BITRATE_EXCEEDED "
                        f"camera={camera} path={source_video} bytes={source_video.stat().st_size} "
                        f"duration_sec={duration_sec:.3f} average_mbps={average_bitrate / 1_000_000:.3f} "
                        "limit_mbps=8.000 suggestion=rerun_with_--repair-video")
                source_info = probe_video(source_video)
                identity = len(source_indices) == len(entries) and np.array_equal(
                    source_indices, np.arange(len(entries), dtype=np.int64)) and \
                    source_info.frame_count == len(entries)
                compliance_issues = video_compliance_issues(source_info)
                if compliance_issues and not repair_video:
                    raise ValueError(
                        "VIDEO_ENCODING_NONCOMPLIANT "
                        f"camera={camera} path={source_video} issues={','.join(compliance_issues)} "
                        "suggestion=rerun_with_--repair-video")

                requires_repair = abnormal_bitrate or bool(compliance_issues)
                if identity and not requires_repair:
                    selected_video = source_video
                    video_infos[camera] = source_info
                else:
                    if requires_repair:
                        target_repair_dir = Path(repair_dir) if repair_dir else output_dir / "repaired"
                        selected_video = target_repair_dir / session_id / camera_dir / "video.mp4"
                        print(
                            f"  [VIDEO_REPAIR_TRANSCODE] camera={camera} "
                            f"average_mbps={average_bitrate / 1_000_000:.3f} "
                            f"issues={','.join(compliance_issues) or 'bitrate'} output={selected_video}")
                    else:
                        selected_video = Path(alignment_temp.name) / camera / "video.mp4"
                        print(
                            f"  [ALIGNMENT_REENCODE] camera={camera} "
                            f"source_frames={len(entries)} output_frames={num_frames} "
                            f"duplicates={int(np.count_nonzero(duplicate))}")
                    video_infos[camera] = align_video(source_video, selected_video, source_indices)
                    output_issues = video_compliance_issues(video_infos[camera])
                    if output_issues:
                        raise ValueError(
                            "VIDEO_DERIVED_NONCOMPLIANT "
                            f"camera={camera} path={selected_video} issues={','.join(output_issues)}")
                    if requires_repair:
                        output_duration = video_infos[camera].duration_sec
                        output_bitrate = (
                            selected_video.stat().st_size * 8.0 / output_duration
                            if output_duration > 0.0 else 0.0)
                        print(
                            f"  [VIDEO_REPAIR_COMPLETE] session={session_id} camera={camera} "
                            f"source_bytes={source_video.stat().st_size} "
                            f"output_bytes={selected_video.stat().st_size} "
                            f"source_mbps={average_bitrate / 1_000_000:.3f} "
                            f"output_mbps={output_bitrate / 1_000_000:.3f} "
                            f"source_duration_sec={duration_sec:.3f} "
                            f"output_duration_sec={output_duration:.3f} "
                            f"source_frames={source_info.frame_count} "
                            f"output_frames={video_infos[camera].frame_count} "
                            f"duplicates={stats['duplicate_frames']} "
                            f"dropped={stats['dropped_source_frames']}")
                check_video_memory_limits(
                    camera,
                    selected_video,
                    warn_video_bytes,
                    max_video_bytes,
                    memory_peak_factor,
                    memory_reserve_bytes)
                video_paths[camera] = selected_video
                print(
                    f"  [ALIGNMENT_SUMMARY] session={session_id} camera={camera} "
                    f"original={stats['original_frames']} aligned={stats['aligned_frames']} "
                    f"duplicates={stats['duplicate_frames']} "
                    f"dropped={stats['dropped_source_frames']} unmatched=0 "
                    f"max_skew_ms={stats['max_skew_ms']:.3f}")

        # 创建 HDF5
        hdf5_path = output_dir / f"{session_id}.hdf5"
        partial_path = output_dir / f"{session_id}.hdf5.partial"
        print(f"  创建 HDF5: {hdf5_path}")

        partial_path.unlink(missing_ok=True)
        try:
            with h5py.File(partial_path, "w") as h5:
                # 全局属性
                h5.attrs["alignment_method"] = "nearest_timestamp"
                h5.attrs["cameras"] = json.dumps(selected_cameras)
                h5.attrs["description"] = "Auto mode recording from dexe_recorder"
                h5.attrs["frames"] = num_frames
                h5.attrs["sample_rate"] = 30.0
                for attr_name, attr_value in airs_root_attrs.items():
                    h5.attrs[attr_name] = attr_value

                # 参考相机时间戳转纳秒（所有相机和关节共用）
                ref_ts_ns = np.array(
                    [unix_to_uint64_ns(ts) for ts in ref_timestamps], dtype=np.uint64)

                # === 相机数据 ===
                for cam_type in selected_cameras:
                    cam_entries = camera_groups[cam_type]
                    hdf5_name = f"camera_{cam_type}"
                    cam_group = h5.create_group(hdf5_name)
                    cam_group.attrs["type"] = "image"

                    # 获取图像尺寸
                    shape = None
                    if fmt == "jpeg":
                        first_path = cam_entries[0].get("image_path", "")
                        shape = get_image_shape(data_dir, first_path)
                    else:
                        info = video_infos[cam_type]
                        shape = (info.height, info.width, 3)

                    if shape is None:
                        shape = (1080, 1920, 3)
                        print(f"  [WARN] {cam_type}: 无法获取尺寸，用默认 {shape}")

                    height, width, channels = shape
                    cam_group.attrs["height"] = height
                    cam_group.attrs["width"] = width
                    cam_group.attrs["channels"] = channels
                    cam_group.attrs["frames"] = num_frames
                    cam_group.attrs["sample_rate"] = 30.0

                    # 时间戳（所有相机用参考相机时间戳）
                    cam_group.create_dataset(
                        "timestamps", data=ref_ts_ns, compression="gzip",
                        compression_opts=4)

                    if fmt == "video":
                        # VIDEO 模式：读整个 mp4 文件作为单个 varlen blob
                        cam_group.attrs["encoding"] = "mp4"
                        video_path = video_paths[cam_type]

                        dt = h5py.vlen_dtype(np.dtype("uint8"))
                        data_ds = cam_group.create_dataset("data", shape=(), dtype=dt)
                        mp4_view = np.memmap(video_path, dtype=np.uint8, mode="r")
                        try:
                            data_ds[()] = mp4_view
                        finally:
                            del mp4_view
                        cam_group.attrs["byte_length"] = video_path.stat().st_size
                        source_indices, duplicate, source_timestamps = alignment_plans[cam_type]
                        stats = alignment_stats[cam_type]
                        for stat_name, stat_value in stats.items():
                            cam_group.attrs[f"alignment_{stat_name}"] = stat_value
                        cam_group.create_dataset("alignment_source_indices", data=source_indices)
                        cam_group.create_dataset("alignment_duplicate", data=duplicate)
                        cam_group.create_dataset(
                            "source_timestamps",
                            data=np.asarray(
                                [unix_to_uint64_ns(timestamp) for timestamp in source_timestamps],
                                dtype=np.uint64),
                            compression="gzip",
                            compression_opts=4)
                        print(f"  {cam_type}: {video_path.stat().st_size} bytes MP4")

                    else:
                        # JPEG 模式：逐帧读取 jpg 文件
                        cam_group.attrs["encoding"] = "jpeg"
                        dt = h5py.vlen_dtype(np.dtype("uint8"))
                        data_ds = cam_group.create_dataset(
                            "data", shape=(num_frames,), dtype=dt,
                            compression="gzip", compression_opts=4)

                        for i, entry in enumerate(tqdm(cam_entries, desc=f"  {cam_type}")):
                            image_path = entry.get("image_path", "")
                            full_path = data_dir / image_path
                            if full_path.exists():
                                data_ds[i] = np.frombuffer(
                                    full_path.read_bytes(), dtype=np.uint8)
                            else:
                                data_ds[i] = np.array([], dtype=np.uint8)

                # === 关节数据 ===
                joints_group = h5.create_group("joints")
                joints_group.attrs["type"] = "vector"
                joints_group.attrs["frames"] = num_frames
                joints_group.attrs["sample_rate"] = 30.0
                joints_group.attrs["columns"] = json.dumps(joint_keys)
                joints_group.attrs["end_effector_value_range"] = airs_root_attrs[
                    "ee_value_range"]

                joints_group.create_dataset(
                    "data", data=qpos_interp, compression="gzip", compression_opts=4)
                joints_group.create_dataset(
                    "timestamps", data=ref_ts_ns, compression="gzip",
                    compression_opts=4)

                # === 反馈数据（可选，保持与动作数据独立） ===
                if feedback_interp is not None and feedback_keys:
                    feedback_group = h5.create_group("feedback")
                    feedback_group.attrs["type"] = "vector"
                    feedback_group.attrs["frames"] = num_frames
                    feedback_group.attrs["sample_rate"] = 30.0
                    feedback_group.attrs["columns"] = json.dumps(feedback_keys)
                    feedback_group.create_dataset(
                        "data", data=feedback_interp, compression="gzip", compression_opts=4)
                    feedback_group.create_dataset(
                        "timestamps", data=ref_ts_ns, compression="gzip", compression_opts=4)

                # === 原始辅助文件存储（反转工具依赖） ===
                # metadata.jsonl：整体存为 bytes dataset
                metadata_path = data_dir / "metadata.jsonl"
                if metadata_path.is_file():
                    with open(metadata_path, "rb") as mf:
                        h5.create_dataset(
                            "metadata_jsonl",
                            data=np.frombuffer(mf.read(), dtype=np.uint8))

                # tactile.jsonl：存在则存，不存在跳过
                tactile_path = data_dir / "tactile.jsonl"
                if tactile_path.is_file():
                    with open(tactile_path, "rb") as tf:
                        h5.create_dataset(
                            "tactile_jsonl",
                            data=np.frombuffer(tf.read(), dtype=np.uint8))

                # subdirs：扫描录制目录所有子目录相对路径（含空目录如 hand/left）
                subdirs = []
                for p in sorted(data_dir.rglob("*")):
                    if p.is_dir():
                        subdirs.append(p.relative_to(data_dir).as_posix())
                h5.attrs["subdirs"] = json.dumps(subdirs)

            validate_hdf5_structure(partial_path, selected_cameras, num_frames)
            os.replace(partial_path, hdf5_path)
        except Exception:
            partial_path.unlink(missing_ok=True)
            raise

        file_size = hdf5_path.stat().st_size / (1024 * 1024)
        print(f"  ✓ {session_id}: {num_frames} 帧, {len(selected_cameras)} 相机, "
              f"{file_size:.1f}MB")
        return True
    finally:
        alignment_temp.cleanup()


def main():
    parser = argparse.ArgumentParser(description="dexe_recorder 录制数据转 HDF5")
    parser.add_argument("--input", required=True, help="录制数据目录（含多个 session 子目录，或单个 session 目录）")
    parser.add_argument("--output", required=True, help="HDF5 输出目录")
    parser.add_argument("--format", default="auto", choices=["auto", "video", "jpeg"],
                        help="录制格式：auto（自动检测）、video、jpeg（默认 auto）")
    parser.add_argument("--skip-existing", action="store_true", help="跳过已存在的 HDF5 文件")
    parser.add_argument("--repair-video", action="store_true",
                        help="显式修复异常码率视频；原片不覆盖")
    parser.add_argument("--repair-dir", help="修复视频输出目录（默认 <output>/repaired）")
    parser.add_argument(
        "--warn-video-mib", type=float,
        default=DEFAULT_WARN_VIDEO_BYTES / 1024 / 1024,
        help="单路视频大小预警门槛 MiB（默认 512；0 表示禁用固定预警）")
    parser.add_argument(
        "--max-video-mib", type=float,
        default=DEFAULT_MAX_VIDEO_BYTES / 1024 / 1024,
        help="单路视频固定硬限制 MiB（默认 2048；0 表示禁用固定硬限制）")
    parser.add_argument("--memory-peak-factor", type=float, default=DEFAULT_MEMORY_PEAK_FACTOR,
                        help="预计峰值内存/单路文件大小系数（默认 4.0；0 表示禁用动态门槛）")
    parser.add_argument("--memory-reserve-mib", type=float,
                        default=DEFAULT_MEMORY_RESERVE_BYTES / 1024 / 1024,
                        help="动态内存门槛保留余量 MiB（默认 512）")
    args = parser.parse_args()

    for option_name in (
        "warn_video_mib",
        "max_video_mib",
        "memory_peak_factor",
        "memory_reserve_mib",
    ):
        if getattr(args, option_name) < 0:
            parser.error(f"--{option_name.replace('_', '-')} 不能为负数")

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.exists():
        print(f"[ERROR] 输入目录不存在: {input_dir}")
        sys.exit(1)

    sessions = find_valid_sessions(input_dir)
    if not sessions:
        print(f"[ERROR] 未找到有效 session 目录: {input_dir}")
        sys.exit(1)

    print(f"找到 {len(sessions)} 个 session")
    success = 0
    skipped = 0
    failed = 0

    for session_dir in sessions:
        session_id = session_dir.name
        hdf5_path = output_dir / f"{session_id}.hdf5"

        if args.skip_existing and hdf5_path.exists():
            if is_valid_existing_hdf5(hdf5_path):
                print(f"[SKIP] {session_id}: 已存在且结构完整")
                skipped += 1
                continue
            print(f"[WARN] {session_id}: 既有 HDF5 不完整，将通过 .partial 重新生成")

        print(f"\n处理: {session_id}")
        try:
            if process_session(
                session_dir,
                output_dir,
                args.format,
                repair_video=args.repair_video,
                repair_dir=Path(args.repair_dir) if args.repair_dir else None,
                warn_video_bytes=int(args.warn_video_mib * 1024 * 1024),
                max_video_bytes=int(args.max_video_mib * 1024 * 1024),
                memory_peak_factor=args.memory_peak_factor,
                memory_reserve_bytes=int(args.memory_reserve_mib * 1024 * 1024),
            ):
                success += 1
        except Exception as error:
            failed += 1
            print(f"  [ERROR] session={session_id} conversion_failed={error}")

    print(f"\n完成: {success}/{len(sessions)} 成功, {skipped} 跳过, {failed} 失败")
    if success > 0:
        print(f"HDF5 文件在: {output_dir}")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
