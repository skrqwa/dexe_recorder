#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""批量转换脚本：遍历 input 路径下的所有数据文件夹，转换为标准 HDF5 格式。

支持动态检测相机类型：根据 metadata.jsonl 中的 camera_type 字段动态保存对应的相机数据。
最少只需要一个相机，最多支持任意数量的相机。

独立工具包，不依赖 src/ 源码树。GStreamer 硬编码器从同目录导入。
"""

import os
import bisect
import concurrent.futures
import json
import multiprocessing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import yaml
import argparse
import numpy as np
import cv2
import h5py
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional, Set
from tqdm import tqdm
from enum import Enum

import logging

# 同目录导入 GStreamer 硬编码器（不依赖 src/ 源码树）
import sys as _sys

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPT_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

# Create a custom logger
logger = logging.getLogger(__name__)

# Set the default log level
logger.setLevel(logging.INFO)


def decorate_str_color(msg: str, color: str):
    """Decorate a string with a specific color."""
    color_map = {
        "red": "\033[91m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "blue": "\033[94m",
        "purple": "\033[95m",
        "cyan": "\033[96m",
        "orange": "\033[33m",
        "white": "\033[97m",
    }
    return f"{color_map.get(color, '')}{msg}\033[0m" if color else msg


def set_log_level(level: str):
    """Set the logging level."""
    level = level.upper()
    assert level in ["DEBUG", "INFO", "WARNING", "ERROR"], "Invalid log level"
    logger.setLevel(getattr(logging, level))


def format_message(level: str, message: str):
    """Format the log message with a consistent prefix."""
    return f"[DexEChain {level}]: {message}"


def log_info(message, color=None):
    """Log an info message."""
    logger.info(decorate_str_color(format_message("INFO", message), color))


def log_debug(message, color="blue"):
    """Log a debug message."""
    logger.debug(decorate_str_color(format_message("DEBUG", message), color))


def log_warning(message):
    """Log a warning message."""
    logger.warning(decorate_str_color(format_message("WARNING", message), "purple"))


def log_error(message, error_type=RuntimeError):
    """Log an error message."""
    raise error_type(decorate_str_color(format_message("ERROR", message), "red"))


class TeleoperationData(Enum):
    """Enum for teleoperation data conversion script specific string constants"""

    # Camera types
    HEAD_CAMERA = "head"
    HAND_CAMERA = "hand"

    # Camera positions
    LEFT_PLACE = "left"
    RIGHT_PLACE = "right"

    # Camera name prefixes
    CAM_HIGH_PREFIX = "cam_high"
    CAM_HAND_PREFIX = "cam_hand"

    # File names and patterns
    METADATA_FILE = "metadata.jsonl"
    QPOS_PATTERN = "pose_record_*.json"
    FEEDBACK_FILE = "feedback.json"
    TACTILE_JSONL_FILE = "tactile.jsonl"
    TACTILE_JSON_FILE = "tactile.json"
    IMAGE_PATH_KEY = "image_path"
    TIMESTAMP_KEY = "timestamp"
    CAMERA_TYPE_KEY = "camera_type"

    # Data structure keys
    OBSERVATIONS = "observations"
    IMAGES = "images"
    QPOS = "qpos"
    ACTION = "action"
    FRAMES = "frames"
    FRAME_ID = "frame_id"
    FRAME_COUNT = "frame_count"
    LANGUAGE = "language_prompt"
    DATA = "data"
    CHUNKS = "chunks"

    # Joint keys (common ones)
    LEFT_GRIPPER = "LEFT_GRIPPER"
    RIGHT_GRIPPER = "RIGHT_GRIPPER"
    LEFT_HAND_PREFIX = "LEFT_HAND"
    RIGHT_HAND_PREFIX = "RIGHT_HAND"
    # Joint index mapping for real robot data
    LEFT_ARM_QPOS_INDICES = [6, 7, 8, 9, 10, 11, 12]
    RIGHT_ARM_QPOS_INDICES = [14, 15, 16, 17, 18, 19, 20]

    LEFT_EEF_DEXTROUSHAND_INDICES = [22, 23, 24, 25, 26, 27]
    RIGHT_EEF_DEXTROUSHAND_INDICES = [28, 29, 30, 31, 32, 33]
    LEFT_EEF_GRIPPER_INDICES = [13]
    RIGHT_EEF_GRIPPER_INDICES = [21]
    WAIST_QPOS_INDICES = [
        3,
    ]
    HEAD_QPOS_INDICES = [4, 5]


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    default_config = {
        "hdf5_attributes":
            {
                "description": "Standard AIRS HDF5 file for teleoperation data",
                "robot_type": "W1_Pro",
                "series_number": "TELEOP001",
                "sample_rate": 30.0,
                "alignment_method": "nearest_timestamp",
            },
        "camera_groups":
            {
                # 这个配置现在仅用于映射camera_type到HDF5中的组名
                "head_left": "camera_head_left",
                "head_right": "camera_head_right",
                "hand_left": "camera_hand_left",
                "hand_right": "camera_hand_right",
            },
        "joint_group": {
            "name": "joints"
        },
        "image_quality": 70
    }

    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
            # 合并配置，用户配置覆盖默认配置
            for key in default_config:
                if key in user_config:
                    if isinstance(default_config[key], dict) and isinstance(user_config[key], dict):
                        default_config[key].update(user_config[key])
                    else:
                        default_config[key] = user_config[key]

    return default_config


def unix_to_uint64_ns(unix_timestamp: float) -> np.uint64:
    ns = int(unix_timestamp * 1e9)
    return np.uint64(ns)


def extract_frame_number(image_path: str) -> int:
    filename = image_path.split("/")[-1]
    frame_number = int(Path(filename).stem)
    return frame_number


def _choose_reference_camera(camera_data_by_type: Dict[str, List[Dict[str, Any]]]) -> str:
    '''
    选择参考相机的策略：优先选择头部左侧相机，如果不存在则选择其他左侧相机，最后选择任意相机。
    这个策略假设头部左侧相机通常是最稳定和连续的，适合作为对齐参考。
    '''
    preferred_left_camera = (TeleoperationData.HEAD_CAMERA.value + "_" + TeleoperationData.LEFT_PLACE.value)
    if preferred_left_camera in camera_data_by_type:
        return preferred_left_camera

    for camera_type in sorted(camera_data_by_type.keys()):
        if camera_type.endswith("_left"):
            return camera_type

    return next(iter(camera_data_by_type.keys()))


def _find_nearest_record_index(ts_list: List[float], ref_ts: float) -> int:
    '''
    在ts_list中找到与ref_ts最近的时间戳索引，使用二分查找实现高效查找。
    '''
    insert_pos = bisect.bisect_left(ts_list, ref_ts)

    if insert_pos <= 0:
        return 0
    if insert_pos >= len(ts_list):
        return len(ts_list) - 1

    prev_idx = insert_pos - 1
    next_idx = insert_pos
    prev_diff = abs(ts_list[prev_idx] - ref_ts)
    next_diff = abs(ts_list[next_idx] - ref_ts)
    return prev_idx if prev_diff <= next_diff else next_idx


def _align_records_by_nearest_timestamp(
    camera_data_by_type: Dict[str, List[Dict[str, Any]]],
    ref_camera: str,
) -> Dict[int, Dict[str, Any]]:
    '''
    按最近时间戳对齐各相机的数据，以参考相机为基准。
    '''
    frame_data: Dict[int, Dict[str, Any]] = {}
    ref_records = camera_data_by_type[ref_camera]
    per_cam_ts = {
        camera_type: [entry[TeleoperationData.TIMESTAMP_KEY.value] for entry in entries]
        for camera_type, entries in camera_data_by_type.items()
    }

    for frame_idx, ref_entry in enumerate(ref_records):
        ref_ts = ref_entry[TeleoperationData.TIMESTAMP_KEY.value]
        frame_data[frame_idx] = {}
        for camera_type, entries in camera_data_by_type.items():
            if camera_type == ref_camera:
                frame_data[frame_idx][camera_type] = ref_entry
                continue

            nearest_idx = _find_nearest_record_index(per_cam_ts[camera_type], ref_ts)
            frame_data[frame_idx][camera_type] = entries[nearest_idx]

    return frame_data


def parse_metadata_aligned(metadata_path: Path,) -> Tuple[Dict[int, Dict[str, Any]], Set[str], str]:
    """读取 metadata.jsonl 并校验各相机帧严格对齐（与算法版本一致）。

    校验规则：
    1. 各相机帧数必须相等（否则报错）
    2. 各相机同一帧的图片文件名必须一致（否则报错）
    不做最近时间戳匹配，因为后处理保存的数据已经对齐。
    """
    # 用于按相机类型分组存储数据
    camera_data_by_type = {}
    detected_camera_types = set()

    # 第一步：读取所有数据并按相机类型分组
    with open(metadata_path, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line.strip())
            timestamp = float(entry[TeleoperationData.TIMESTAMP_KEY.value])
            camera_type = entry[TeleoperationData.CAMERA_TYPE_KEY.value]
            image_path = entry[TeleoperationData.IMAGE_PATH_KEY.value]

            if camera_type not in camera_data_by_type:
                camera_data_by_type[camera_type] = []
            camera_data_by_type[camera_type].append(
                {
                    "path": image_path,
                    TeleoperationData.TIMESTAMP_KEY.value: timestamp,
                    TeleoperationData.CAMERA_TYPE_KEY.value: camera_type,
                })

            detected_camera_types.add(camera_type)

    # 对每个相机类型的数据按照timestamp排序
    for camera_type in camera_data_by_type:
        camera_data_by_type[camera_type].sort(key=lambda x: x[TeleoperationData.TIMESTAMP_KEY.value])
        log_info(f"已对相机类型 '{camera_type}' 的数据按时间戳排序，共 {len(camera_data_by_type[camera_type])} 条记录")

    if not camera_data_by_type:
        return {}, detected_camera_types, ""

    ref_camera = _choose_reference_camera(camera_data_by_type)
    log_info(f"对齐模式: strict_alignment, 参考相机: {ref_camera}")

    # 校验：各相机帧数必须相等，且图片名逐一对应（与算法版本 validate_rgb_metadata_alignment 一致）
    ref_records = camera_data_by_type[ref_camera]
    ref_pic_names = [Path(r["path"]).name for r in ref_records]

    for camera_type, entries in camera_data_by_type.items():
        if camera_type == ref_camera:
            continue
        if len(entries) != len(ref_records):
            raise ValueError(
                f"相机帧数不一致: {camera_type}={len(entries)}, {ref_camera}={len(ref_records)}")
        mismatches = []
        for i, (ref_name, entry) in enumerate(zip(ref_pic_names, entries)):
            pic_name = Path(entry["path"]).name
            if pic_name != ref_name:
                mismatches.append(f"frame {i}: {ref_camera}={ref_name}, {camera_type}={pic_name}")
                if len(mismatches) >= 5:
                    break
        if mismatches:
            raise ValueError(f"图片名不一致: {camera_type} vs {ref_camera}; {mismatches}")

    # 所有相机已严格对齐，直接按帧索引构建 frame_data
    frame_data: Dict[int, Dict[str, Any]] = {}
    for frame_idx in range(len(ref_records)):
        frame_data[frame_idx] = {}
        for camera_type, entries in camera_data_by_type.items():
            frame_data[frame_idx][camera_type] = entries[frame_idx]

    return frame_data, detected_camera_types, ref_camera


def find_valid_frames(
    frame_data: Dict[int, Dict[str, Any]],
    required_camera_types: Optional[Set[str]] = None,
) -> List[int]:
    """所有帧都有效（已严格校验对齐），直接返回全部帧索引。"""
    return sorted(frame_data.keys())


def load_qpos_and_joint_keys(qpos_path: Path,) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    with open(qpos_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    frames = data.get(TeleoperationData.FRAMES.value, [])
    if not frames:
        return np.array([]), np.array([]), []

    # 按时间戳排序
    frames.sort(key=lambda x: x[TeleoperationData.TIMESTAMP_KEY.value])

    first_frame_data = frames[0][TeleoperationData.DATA.value]
    joint_keys = list(first_frame_data.keys())

    ts = np.array([f[TeleoperationData.TIMESTAMP_KEY.value] for f in frames], dtype=np.float64)

    qpos_list = []
    for f in frames:
        qpos_values = []
        for key in joint_keys:
            qpos_values.append(f[TeleoperationData.DATA.value].get(key, 0.0))
        qpos_list.append(qpos_values)

    qpos = np.array(qpos_list, dtype=np.float32)
    return ts, qpos, joint_keys


def _unix_timestamp_to_seconds(timestamp: Any) -> float:
    """将 Unix 秒、毫秒、微秒或纳秒时间戳统一转换为秒。"""
    value = float(timestamp)
    magnitude = abs(value)
    if magnitude >= 1e17:
        return value / 1e9
    if magnitude >= 1e14:
        return value / 1e6
    if magnitude >= 1e11:
        return value / 1e3
    return value


def _flatten_feedback_frame(frame: Dict[str, Any], frame_index: int) -> Dict[str, float]:
    """提取一帧 robot_state 与左右 EE 的 joints，并拒绝重名字段。"""
    data = frame.get(TeleoperationData.DATA.value, {})
    sources = [
        ("robot_state", data.get("robot_state", {}).get("joints", {})),
        ("ee.left", data.get("ee", {}).get("left", {}).get("joints", {})),
        ("ee.right", data.get("ee", {}).get("right", {}).get("joints", {})),
    ]

    flattened: Dict[str, float] = {}
    owners: Dict[str, str] = {}
    for source_name, joints in sources:
        if not isinstance(joints, dict):
            raise ValueError(
                "FEEDBACK_JOINTS_INVALID_TYPE "
                f"frame={frame_index} source={source_name} expected=dict")
        for joint_name, value in joints.items():
            if joint_name in flattened:
                raise ValueError(
                    "FEEDBACK_JOINT_NAME_CONFLICT "
                    f"frame={frame_index} joint={joint_name} "
                    f"sources={owners[joint_name]},{source_name}")
            if not isinstance(value, (int, float)):
                raise ValueError(
                    "FEEDBACK_JOINT_VALUE_INVALID "
                    f"frame={frame_index} source={source_name} joint={joint_name}")
            flattened[joint_name] = float(value)
            owners[joint_name] = source_name
    return flattened


def load_feedback_and_joint_keys(
    feedback_path: Path,
) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """读取嵌套 feedback，按外层时间戳整理成与 pose 相同的二维向量。"""
    with open(feedback_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    frames = payload.get(TeleoperationData.FRAMES.value, [])
    if not frames:
        return np.array([]), np.array([]), []

    normalized_frames = []
    for frame_index, frame in enumerate(frames):
        timestamp = _unix_timestamp_to_seconds(
            frame[TeleoperationData.TIMESTAMP_KEY.value])
        normalized_frames.append(
            (timestamp, frame_index, _flatten_feedback_frame(frame, frame_index)))
    normalized_frames.sort(key=lambda item: item[0])

    joint_keys = list(normalized_frames[0][2].keys())
    expected_keys = set(joint_keys)
    feedback_rows = []
    timestamps = []
    for timestamp, frame_index, values in normalized_frames:
        actual_keys = set(values.keys())
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise ValueError(
                "FEEDBACK_JOINT_SCHEMA_CHANGED "
                f"frame={frame_index} missing={missing} extra={extra}")
        timestamps.append(timestamp)
        feedback_rows.append([values[key] for key in joint_keys])

    return (
        np.asarray(timestamps, dtype=np.float64),
        np.asarray(feedback_rows, dtype=np.float32),
        joint_keys,
    )


def find_feedback_file(data_dir: Path) -> Optional[Path]:
    """查找遥操录制端生成的可选 feedback.json。"""
    feedback_path = data_dir / TeleoperationData.FEEDBACK_FILE.value
    return feedback_path if feedback_path.is_file() else None


def _unix_timestamp_to_uint64_ns(timestamp: Any) -> np.uint64:
    """按数值量级识别 Unix 时间戳单位并精确转换为整数纳秒。"""
    try:
        value = Decimal(str(timestamp))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"TACTILE_TIMESTAMP_INVALID value={timestamp}") from exc
    magnitude = abs(value)
    if magnitude >= Decimal("1e17"):
        multiplier = Decimal("1")
    elif magnitude >= Decimal("1e14"):
        multiplier = Decimal("1e3")
    elif magnitude >= Decimal("1e11"):
        multiplier = Decimal("1e6")
    else:
        multiplier = Decimal("1e9")
    nanoseconds = int((value * multiplier).to_integral_value(rounding=ROUND_HALF_UP))
    if nanoseconds <= 0:
        raise ValueError(f"TACTILE_TIMESTAMP_INVALID value={timestamp}")
    return np.uint64(nanoseconds)


def _parse_tactile_records(records: List[Tuple[str, Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """将左右手触觉记录分别整理为保持原采样率的向量数据。"""
    axes = ("x", "y", "z")
    active_hands = {
        record.get("hand")
        for _, record in records
        if isinstance(record.get("tactile_states"), list)
        and any(
            isinstance(state, dict) and state.get("distributed_datas")
            for state in record["tactile_states"]
        )
    }
    hands: Dict[str, Dict[str, Any]] = {}
    for source, record in records:
        hand = record.get("hand")
        states = record.get("tactile_states", [])
        if not isinstance(states, list):
            raise ValueError(f"TACTILE_STATES_INVALID_TYPE source={source}")

        states_with_data = [state for state in states if state.get("distributed_datas")]
        if not states_with_data:
            if hand in active_hands:
                raise ValueError(
                    f"TACTILE_EMPTY_SAMPLE source={source} hand={hand}")
            continue
        if hand not in ("left", "right"):
            raise ValueError(f"TACTILE_HAND_INVALID source={source} hand={hand}")
        if len(states_with_data) != len(states):
            raise ValueError(f"TACTILE_PARTIAL_FIELD_DATA source={source} hand={hand}")

        field_names = [str(state.get("field_name", "")) for state in states]
        if any(not name for name in field_names):
            raise ValueError(f"TACTILE_FIELD_NAME_EMPTY source={source} hand={hand}")
        if len(field_names) != len(set(field_names)):
            raise ValueError(f"TACTILE_FIELD_NAME_DUPLICATE source={source} hand={hand}")

        category_values = {"force": [], "torque": []}
        category_presence = {"force": [], "torque": []}
        for state in states:
            distributed = state["distributed_datas"]
            row = int(state.get("row", 0))
            col = int(state.get("col", 0))
            if row != 1 or col != 1 or len(distributed) != 1:
                raise ValueError(
                    "TACTILE_ARRAY_UNSUPPORTED "
                    f"source={source} hand={hand} field={state.get('field_name')} "
                    f"row={row} col={col} points={len(distributed)}")
            point = distributed[0]
            for category in category_values:
                present = category in point
                category_presence[category].append(present)
                if present:
                    vector = point[category]
                    if not isinstance(vector, dict) or any(axis not in vector for axis in axes):
                        raise ValueError(
                            "TACTILE_VECTOR_INVALID "
                            f"source={source} hand={hand} field={state.get('field_name')} "
                            f"category={category}")
                    values = [float(vector[axis]) for axis in axes]
                    if not all(np.isfinite(value) for value in values):
                        raise ValueError(
                            "TACTILE_VALUE_NONFINITE "
                            f"source={source} hand={hand} field={state.get('field_name')} "
                            f"category={category}")
                    category_values[category].extend(values)

        categories = {}
        for category, presence in category_presence.items():
            if any(presence) and not all(presence):
                raise ValueError(
                    f"TACTILE_CATEGORY_SCHEMA_CHANGED source={source} hand={hand} "
                    f"category={category}")
            if all(presence):
                categories[category] = {
                    "columns": [f"{field}_{axis}" for field in field_names for axis in axes],
                    "values": category_values[category],
                }
        if not categories:
            continue

        timestamp_ns = _unix_timestamp_to_uint64_ns(record.get("timestamp"))
        hand_data = hands.setdefault(
            hand,
            {
                "timestamps_ns": [],
                "columns": {name: data["columns"] for name, data in categories.items()},
                "rows": {name: [] for name in categories},
            },
        )
        actual_columns = {name: data["columns"] for name, data in categories.items()}
        if actual_columns != hand_data["columns"]:
            raise ValueError(f"TACTILE_SCHEMA_CHANGED source={source} hand={hand}")
        if hand_data["timestamps_ns"] and timestamp_ns < hand_data["timestamps_ns"][-1]:
            raise ValueError(f"TACTILE_TIMESTAMP_NON_MONOTONIC source={source} hand={hand}")
        hand_data["timestamps_ns"].append(timestamp_ns)
        for category, data in categories.items():
            hand_data["rows"][category].append(data["values"])

    for hand_data in hands.values():
        hand_data["timestamps_ns"] = np.asarray(hand_data["timestamps_ns"], dtype=np.uint64)
        hand_data["data"] = {
            category: np.asarray(rows, dtype=np.float32)
            for category, rows in hand_data.pop("rows").items()
        }
    return hands


def load_tactile_jsonl(tactile_path: Path) -> Dict[str, Dict[str, Any]]:
    """读取旧版逐行 tactile.jsonl，空文件表示没有触觉数据。"""
    records: List[Tuple[str, Dict[str, Any]]] = []
    with open(tactile_path, "r", encoding="utf-8") as tactile_file:
        for line_number, line in enumerate(tactile_file, 1):
            if not line.strip():
                raise ValueError(f"TACTILE_JSONL_EMPTY_LINE line={line_number}")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"TACTILE_JSONL_INVALID line={line_number}") from exc
            record["timestamp"] = record.get("ts")
            records.append((f"line={line_number}", record))
    return _parse_tactile_records(records)


def load_tactile_json(tactile_path: Path) -> Dict[str, Dict[str, Any]]:
    """读取新版结构化 tactile.json，并使用各手侧内部的源时间戳。"""
    with open(tactile_path, "r", encoding="utf-8") as tactile_file:
        payload = json.load(tactile_file)
    frames = payload.get(TeleoperationData.FRAMES.value, [])
    if not isinstance(frames, list):
        raise ValueError("TACTILE_FRAMES_INVALID_TYPE expected=list")

    records: List[Tuple[str, Dict[str, Any]]] = []
    for frame_index, frame in enumerate(frames):
        frame_data = frame.get(TeleoperationData.DATA.value, {})
        if not isinstance(frame_data, dict):
            raise ValueError(f"TACTILE_FRAME_DATA_INVALID frame={frame_index}")
        for hand in ("left", "right"):
            if hand not in frame_data:
                continue
            side_data = frame_data[hand]
            if not isinstance(side_data, dict):
                raise ValueError(
                    f"TACTILE_HAND_DATA_INVALID frame={frame_index} hand={hand}")
            records.append(
                (
                    f"frame={frame_index}",
                    {
                        "hand": hand,
                        "timestamp": side_data.get("timestamp"),
                        "tactile_states": side_data.get("tactile_states", []),
                    },
                )
            )
    return _parse_tactile_records(records)


def find_tactile_file(data_dir: Path) -> Tuple[Optional[str], Optional[Path]]:
    """查找唯一触觉输入文件，避免新旧格式同时存在时产生歧义。"""
    jsonl_path = data_dir / TeleoperationData.TACTILE_JSONL_FILE.value
    json_path = data_dir / TeleoperationData.TACTILE_JSON_FILE.value
    existing = [path for path in (jsonl_path, json_path) if path.is_file()]
    if len(existing) > 1:
        raise ValueError(
            "TACTILE_MULTIPLE_INPUT_FILES files="
            + ",".join(path.name for path in existing))
    if not existing:
        return None, None
    path = existing[0]
    return ("jsonl" if path.suffix == ".jsonl" else "json"), path


def load_language(qpos_path: Path) -> str:
    with open(qpos_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get(TeleoperationData.LANGUAGE.value, "")


def interp_qpos(qpos_ts: np.ndarray, qpos: np.ndarray, target_ts: np.ndarray) -> np.ndarray:
    D = qpos.shape[1]
    out = np.zeros((len(target_ts), D), dtype=np.float32)
    for d in range(D):
        out[:, d] = np.interp(target_ts, qpos_ts, qpos[:, d])
    return out


def encode_image_to_jpeg(image: np.ndarray, jpeg_quality: int = 95) -> bytes:
    if len(image.shape) == 3 and image.shape[2] == 3:
        # 假设输入是BGR格式（OpenCV默认）
        image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    else:
        image_rgb = image

    success, encoded_image = cv2.imencode(".jpg", image_rgb, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not success:
        raise ValueError("Failed to encode image to JPEG")

    return encoded_image.tobytes()


def load_image_for_frame(data_dir: Path, image_path: str, jpeg_quality: int = 95) -> Optional[bytes]:
    if not image_path:
        return None

    full_path = data_dir / image_path
    if full_path.exists():
        img = cv2.imread(str(full_path))
        if img is not None:
            return encode_image_to_jpeg(img, jpeg_quality)
        else:
            raise ValueError(f"Failed to load image (corrupted or unsupported format): {full_path}")
    else:
        raise FileNotFoundError(f"Image not found: {full_path}")

    return None


def get_image_shape_from_path(data_dir: Path, image_path: str) -> tuple:
    """从图像路径获取图像形状"""
    if not image_path:
        return None

    full_path = data_dir / image_path
    if full_path.exists():
        img = cv2.imread(str(full_path))
        if img is not None:
            return img.shape
    return None


def create_standard_hdf5(input_dir: Path, output_path: Path, config: Dict[str, Any], fmt: str = "jpeg"):

    data_dir = input_dir
    output_path = output_path
    # 从配置中获取参数
    hdf5_attrs = config.get("hdf5_attributes", {})
    camera_groups = config.get("camera_groups", {})
    joint_config = config.get("joint_group", {})
    image_quality = config.get("image_quality", 95)

    # 检查必要文件
    metadata_path = data_dir / TeleoperationData.METADATA_FILE.value
    if not metadata_path.exists():
        raise FileNotFoundError(f"Metadata file not found: {metadata_path}")

    # 查找qpos文件
    qpos_files = list(data_dir.glob(TeleoperationData.QPOS_PATTERN.value))
    if not qpos_files:
        raise FileNotFoundError(f"No qpos files found matching pattern: {TeleoperationData.QPOS_PATTERN.value}")
    qpos_path = qpos_files[0]

    log_info(f"  Loading metadata from: {metadata_path}")
    frame_data, detected_camera_types, resolved_reference_camera = parse_metadata_aligned(metadata_path,)

    log_info(f"  Detected camera types: {sorted(detected_camera_types)}")
    log_info(f"  Total frames in metadata: {len(frame_data)}")

    # 如果没有检测到任何相机，报错
    if not detected_camera_types:
        raise ValueError("No camera data found in metadata")

    # 使用所有检测到的相机类型
    selected_camera_set = detected_camera_types
    log_info(f"  Using all detected camera types: {sorted(selected_camera_set)}")

    # 查找该相机组合下的有效帧
    valid_frames = find_valid_frames(frame_data, selected_camera_set)

    if not valid_frames:
        # 如果没有包含所有相机的帧，尝试寻找包含至少一个相机的帧
        log_warning("  No frames contain all cameras, trying frames with any camera...")
        valid_frames = find_valid_frames(frame_data, None)
        if not valid_frames:
            raise ValueError("No valid frames found")

    log_info(f"  Valid frames: {len(valid_frames)}")

    # 收集时间戳和图像路径
    timestamps = []
    camera_paths = {camera: [] for camera in selected_camera_set}

    for frame_id in tqdm(valid_frames, desc="  Collecting frame info", leave=False):
        frame_info = frame_data[frame_id]

        # 使用第一个相机的时间戳作为该帧的时间戳
        if resolved_reference_camera in frame_info:
            timestamp = frame_info[resolved_reference_camera]["timestamp"]
        else:
            # 如果第一个相机不存在，使用任意一个相机的时间戳
            any_camera = list(frame_info.keys())[0]
            timestamp = frame_info[any_camera]["timestamp"]
        timestamps.append(timestamp)

        # 收集每个相机的图像路径
        for camera in selected_camera_set:
            if camera in frame_info:
                camera_paths[camera].append(frame_info[camera]["path"])
            else:
                # 如果该帧没有这个相机，保存空字符串
                camera_paths[camera].append("")

    # 加载关节数据
    log_info(f"  Loading qpos data from: {qpos_path}")
    ts_q, qpos, joint_keys = load_qpos_and_joint_keys(qpos_path)

    if len(joint_keys) == 0:
        log_warning("  No joint keys found in qpos data")
        joint_keys = []

    log_info(f"  Found {len(joint_keys)} joint keys: {joint_keys[:5]}...")

    # 加载语言提示
    language = load_language(qpos_path)
    log_info(f"  Language prompt: {language[:50]}...")

    # 插值关节数据到图像时间戳
    if len(joint_keys) > 0:
        log_info("  Interpolating qpos data to image timestamps...")
        qpos_interp = interp_qpos(ts_q, qpos, np.array(timestamps, dtype=np.float64))
    else:
        qpos_interp = np.zeros((len(timestamps), 0), dtype=np.float32)

    # 加载可选反馈数据。上游负责关节命名，本工具只展开嵌套 joints 并对齐。
    feedback_path = find_feedback_file(data_dir)
    feedback_interp = None
    feedback_keys: List[str] = []
    if feedback_path is not None:
        log_info(f"  Loading feedback data from: {feedback_path}")
        ts_feedback, feedback, feedback_keys = load_feedback_and_joint_keys(feedback_path)
        if feedback_keys:
            log_info(
                f"  Interpolating feedback data: {len(ts_feedback)} -> "
                f"{len(timestamps)} frames, {len(feedback_keys)} joints")
            feedback_interp = interp_qpos(
                ts_feedback, feedback, np.array(timestamps, dtype=np.float64))
        else:
            log_warning("  Feedback file contains no joint data; skipping /feedback")

    tactile_format, tactile_path = find_tactile_file(data_dir)
    tactile_hands: Dict[str, Dict[str, Any]] = {}
    if tactile_path is not None:
        log_info(f"  Loading tactile data from: {tactile_path}")
        tactile_hands = (
            load_tactile_jsonl(tactile_path)
            if tactile_format == "jsonl"
            else load_tactile_json(tactile_path)
        )
        for hand, tactile_data in tactile_hands.items():
            log_info(
                f"  Found tactile data: hand={hand}, "
                f"samples={len(tactile_data['timestamps_ns'])}, "
                f"categories={sorted(tactile_data['data'])}")

    # 确定每个相机的图像形状（从第一帧获取）
    camera_shapes = {}
    for camera in selected_camera_set:
        if camera_paths[camera] and camera_paths[camera][0]:
            try:
                shape = get_image_shape_from_path(data_dir, camera_paths[camera][0])
                if shape is not None:
                    camera_shapes[camera] = shape
                    log_info(f"  Camera {camera} shape: {shape}")
                else:
                    # 如果无法获取形状，使用默认值
                    camera_shapes[camera] = (360, 640, 3)
                    log_warning(
                        f"  Could not determine shape for camera {camera}, using default: {camera_shapes[camera]}")
            except Exception as e:
                log_warning(f"  Failed to get image shape for camera {camera}: {e}")
                camera_shapes[camera] = (360, 640, 3)
        else:
            camera_shapes[camera] = (360, 640, 3)
            log_warning(f"  No image path for camera {camera}, using default shape: {camera_shapes[camera]}")

    # 创建HDF5文件
    log_info(f"  Creating HDF5 file: {output_path}")
    with h5py.File(output_path, "w") as h5_file:
        # 设置全局属性 - 按照原始格式
        for key, value in hdf5_attrs.items():
            h5_file.attrs[key] = value

        # 添加额外的全局属性
        h5_file.attrs["frames"] = len(valid_frames)
        h5_file.attrs["cameras"] = json.dumps(sorted(selected_camera_set))
        if language:
            h5_file.attrs["language"] = language
        else:
            h5_file.attrs["language"] = ""

        # 为每个相机创建组
        camera_groups_dict = {}
        for camera_type in selected_camera_set:
            # 获取HDF5中的组名
            hdf5_group_name = camera_groups.get(camera_type, f"camera_{camera_type}")

            # 获取该相机的实际形状
            shape = camera_shapes[camera_type]
            height, width, channels = shape

            # 创建相机组
            camera_group = h5_file.create_group(hdf5_group_name)

            # 设置相机组属性
            camera_group.attrs["type"] = "image"
            if fmt == "video":
                camera_group.attrs["encoding"] = "mp4"
                # 视频模式：单个 varlen blob
                dt = h5py.vlen_dtype(np.dtype('uint8'))
                data_dataset = camera_group.create_dataset("data", shape=(), dtype=dt)
            else:
                camera_group.attrs["encoding"] = "jpeg"
                # JPEG 模式：逐帧 varlen 数组
                dt = h5py.vlen_dtype(np.dtype('uint8'))
                data_dataset = camera_group.create_dataset(
                    "data", shape=(len(valid_frames),), dtype=dt, compression="gzip", compression_opts=4)

            camera_group.attrs["height"] = height
            camera_group.attrs["width"] = width
            camera_group.attrs["channels"] = channels
            camera_group.attrs["frames"] = len(valid_frames)
            camera_group.attrs["sample_rate"] = 30.0

            # 创建timestamps数据集
            timestamps_dataset = camera_group.create_dataset(
                "timestamps", shape=(len(valid_frames),), dtype=np.uint64, compression="gzip", compression_opts=4)

            camera_groups_dict[camera_type] = {
                "group": camera_group,
                "data": data_dataset,
                "timestamps": timestamps_dataset,
                "shape": shape
            }

        # 创建joints组
        if len(joint_keys) > 0:
            joints_group = h5_file.create_group("joints")

            # 设置joints组属性
            joints_group.attrs["type"] = "vector"
            joints_group.attrs["frames"] = len(valid_frames)
            joints_group.attrs["sample_rate"] = 30.0
            joints_group.attrs["columns"] = json.dumps(joint_keys)

            # 创建data数据集
            data_dataset = joints_group.create_dataset("data", data=qpos_interp, compression="gzip", compression_opts=4)

            # 创建timestamps数据集
            timestamps_dataset = joints_group.create_dataset(
                "timestamps", shape=(len(valid_frames),), dtype=np.uint64, compression="gzip", compression_opts=4)

            # 设置joints的timestamps
            timestamps_ns = np.array([unix_to_uint64_ns(ts) for ts in timestamps], dtype=np.uint64)
            timestamps_dataset[:] = timestamps_ns

        # 创建独立 feedback 组，保持与 joints 相同的参考相机时间轴。
        if feedback_interp is not None and feedback_keys:
            feedback_group = h5_file.create_group("feedback")
            feedback_group.attrs["type"] = "vector"
            feedback_group.attrs["frames"] = len(valid_frames)
            feedback_group.attrs["sample_rate"] = 30.0
            feedback_group.attrs["columns"] = json.dumps(feedback_keys)
            feedback_group.create_dataset(
                "data", data=feedback_interp, compression="gzip", compression_opts=4)
            timestamps_ns = np.array(
                [unix_to_uint64_ns(ts) for ts in timestamps], dtype=np.uint64)
            feedback_group.create_dataset(
                "timestamps", data=timestamps_ns, compression="gzip", compression_opts=4)

        # 触觉保持设备原始采样，不插值到相机时间轴。
        if tactile_hands:
            tactile_group = h5_file.create_group("tactile")
            tactile_group.attrs["type"] = "vector"
            tactile_group.attrs["hands"] = json.dumps(sorted(tactile_hands))
            for hand in sorted(tactile_hands):
                tactile_data = tactile_hands[hand]
                tactile_timestamps = tactile_data["timestamps_ns"]
                hand_group = tactile_group.create_group(hand)
                hand_group.attrs["frames"] = len(tactile_timestamps)
                if len(tactile_timestamps) > 1 and tactile_timestamps[-1] > tactile_timestamps[0]:
                    sample_rate = (
                        (len(tactile_timestamps) - 1) * 1e9
                        / float(tactile_timestamps[-1] - tactile_timestamps[0]))
                else:
                    sample_rate = 0.0
                hand_group.attrs["sample_rate"] = sample_rate
                for category in sorted(tactile_data["data"]):
                    category_group = hand_group.create_group(category)
                    category_group.attrs["columns"] = json.dumps(
                        tactile_data["columns"][category])
                    category_group.create_dataset(
                        "data",
                        data=tactile_data["data"][category],
                        compression="gzip",
                        compression_opts=4,
                    )
                    category_group.create_dataset(
                        "timestamps",
                        data=tactile_timestamps,
                        compression="gzip",
                        compression_opts=4,
                    )

        if fmt == "video":
            # ── 视频模式：逐帧推流 → GStreamer GPU 硬编 → MP4，不积压内存 ──
            from PIL import Image
            from gst_h264_encoder import encode_frames_to_mp4

            log_info("  Encoding frames to MP4 video (GStreamer GPU hard-encode)...")
            for camera_type in tqdm(sorted(selected_camera_set), desc="  Encoding cameras to MP4"):
                camera_info = camera_groups_dict[camera_type]
                shape = camera_shapes[camera_type]
                cam_h, cam_w, _ = shape

                def _frame_generator():
                    for image_path in camera_paths[camera_type]:
                        if image_path:
                            full_path = data_dir / image_path
                            if full_path.exists():
                                yield Image.open(full_path).convert("RGB")

                mp4_bytes = encode_frames_to_mp4(
                    _frame_generator(),
                    width=cam_w,
                    height=cam_h,
                    fps=30,
                )
                if mp4_bytes is not None:
                    camera_info["data"][()] = np.frombuffer(mp4_bytes, dtype=np.uint8)
                    log_info(f"  {camera_type}: {len(mp4_bytes)} bytes MP4")
                else:
                    log_warning(f"  {camera_type}: MP4 encoding returned None")

                # 写时间戳
                timestamps_ns = np.array([unix_to_uint64_ns(ts) for ts in timestamps], dtype=np.uint64)
                camera_info["timestamps"][:] = timestamps_ns

        else:
            # ── JPEG 模式（现有逻辑）──
            log_info("  Processing images (streaming JPEG mode)...")
            for frame_idx in tqdm(range(len(valid_frames)), desc="  Writing images to HDF5"):
                frame_timestamp_ns = unix_to_uint64_ns(timestamps[frame_idx])

                for camera_type in selected_camera_set:
                    image_path = camera_paths[camera_type][frame_idx]
                    camera_info = camera_groups_dict[camera_type]

                    if image_path:
                        try:
                            # 加载并编码图像为JPEG字节（保持原始分辨率）
                            encoded_image = load_image_for_frame(data_dir, image_path, image_quality)
                            if encoded_image is not None:
                                # 将JPEG字节写入HDF5
                                camera_info["data"][frame_idx] = np.frombuffer(encoded_image, dtype=np.uint8)
                            else:
                                # 写入空数据
                                camera_info["data"][frame_idx] = np.array([], dtype=np.uint8)
                                log_warning(f"  Empty image for camera {camera_type}, frame {frame_idx}")
                        except Exception as e:
                            log_warning(f"  Failed to load image for camera {camera_type}, frame {frame_idx}: {e}")
                            # 写入空数据
                            camera_info["data"][frame_idx] = np.array([], dtype=np.uint8)
                    else:
                        # 写入空数据
                        camera_info["data"][frame_idx] = np.array([], dtype=np.uint8)

                    # 设置时间戳
                    camera_info["timestamps"][frame_idx] = frame_timestamp_ns

        log_info(f"  Successfully created HDF5 file with {len(valid_frames)} frames (format={fmt})")

        # 存储原始 metadata.jsonl（整体存为 bytes dataset，反转时直接写回）
        _metadata_file = data_dir / TeleoperationData.METADATA_FILE.value
        if _metadata_file.is_file():
            with open(_metadata_file, "rb") as mf:
                h5_file.create_dataset("metadata_jsonl", data=np.frombuffer(mf.read(), dtype=np.uint8))

        # 存储触觉原始文件，保持新旧格式可逆。
        if tactile_path is not None and tactile_format is not None:
            raw_dataset_name = f"tactile_{tactile_format}"
            with open(tactile_path, "rb") as tactile_file:
                h5_file.create_dataset(
                    raw_dataset_name,
                    data=np.frombuffer(tactile_file.read(), dtype=np.uint8),
                )

        # 存储原始目录结构（所有子目录相对路径，用于还原空目录）
        _subdirs = []
        for p in sorted(data_dir.rglob("*")):
            if p.is_dir():
                _subdirs.append(str(p.relative_to(data_dir)))
        h5_file.attrs["subdirs"] = json.dumps(_subdirs)


def process_single_dataset(
    input_dir: Path,
    output_dir: Path,
    config: Dict[str, Any],
    dataset_name: Optional[str] = None,
    fmt: str = "jpeg",
):
    """处理单个数据集文件夹"""
    if dataset_name is None:
        dataset_name = input_dir.name

    output_path = output_dir / f"{dataset_name}.hdf5"

    log_info(f"Processing dataset: {dataset_name} (format={fmt})")
    log_info(f"  Input directory: {input_dir}")
    log_info(f"  Output file: {output_path}")

    try:
        create_standard_hdf5(input_dir, output_path, config, fmt=fmt)
        log_info(f"  ✓ Successfully processed {dataset_name}")
        return True
    except Exception as e:
        log_warning(f"  ✗ Failed to process {dataset_name}: {e}")
        return False


def process_dataset_wrapper(args):
    """包装函数，用于并行处理单个数据集"""
    dataset_dir, output_path, config, skip_existing, dataset_name, fmt = args

    # 重新设置日志（每个进程需要独立的日志设置）
    import logging
    logger = logging.getLogger(__name__)

    try:
        # 检查是否跳过已存在的文件
        output_file = output_path / f"{dataset_name}.hdf5"
        if skip_existing and output_file.exists():
            return {
                "dataset": dataset_name,
                "success": True,
                "skipped": True,
                "message": f"Skipped existing file: {output_file.name}"
            }

        # 调用原有的处理函数
        success = process_single_dataset(dataset_dir, output_path, config, dataset_name, fmt=fmt)

        if success:
            return {
                "dataset": dataset_name,
                "success": True,
                "skipped": False,
                "message": f"Successfully processed {dataset_name}"
            }
        else:
            return {
                "dataset": dataset_name,
                "success": False,
                "skipped": False,
                "message": f"Failed to process {dataset_name}"
            }

    except Exception as e:
        return {
            "dataset": dataset_name,
            "success": False,
            "skipped": False,
            "message": f"Error processing {dataset_name}: {str(e)}"
        }


def main():
    parser = argparse.ArgumentParser(description="批量转换脚本：遍历input路径下的所有数据文件夹，转换为标准HDF5格式")
    parser.add_argument(
        "--input",
        type=str,
        required=True,
        help="输入路径，包含多个数据文件夹",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="输出路径，用于保存HDF5文件",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="配置文件路径（YAML格式）",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="跳过已存在的HDF5文件",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="并行工作进程数（默认：CPU核心数）",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        default=True,
        help="启用并行处理模式(默认启用)",
    )
    parser.add_argument(
        "--max-queue-size",
        type=int,
        default=100,
        help="任务队列最大大小",
    )
    parser.add_argument(
        "--format",
        type=str,
        default="jpeg",
        choices=["jpeg", "video"],
        help="HDF5 存储格式：jpeg=逐帧JPEG（默认），video=MP4视频流",
    )

    args = parser.parse_args()

    # 设置日志级别
    set_log_level(args.log_level)

    # 加载配置
    config = load_config(args.config)

    input_path = Path(args.input)
    output_path = Path(args.output)

    # 确保输出目录存在
    output_path.mkdir(parents=True, exist_ok=True)

    # 检查输入路径是否存在
    if not input_path.exists():
        log_error(f"Input path does not exist: {input_path}")

    # 查找所有数据集文件夹
    dataset_dirs = []
    if input_path.is_dir():
        # 先检查目录本身是否是 dataset（包含 metadata.jsonl + pose_record）
        self_metadata = input_path / TeleoperationData.METADATA_FILE.value
        self_qpos = list(input_path.glob(TeleoperationData.QPOS_PATTERN.value))
        if self_metadata.exists() and self_qpos:
            # 目录本身是 dataset，只处理这一个
            dataset_dirs.append(input_path)
        else:
            # 目录不是 dataset，遍历子目录查找
            for item in input_path.iterdir():
                if item.is_dir():
                    metadata_file = item / TeleoperationData.METADATA_FILE.value
                    qpos_files = list(item.glob(TeleoperationData.QPOS_PATTERN.value))
                    if metadata_file.exists() and qpos_files:
                        dataset_dirs.append(item)
    else:
        # 如果是单个数据集文件夹
        metadata_file = input_path / TeleoperationData.METADATA_FILE.value
        qpos_files = list(input_path.glob(TeleoperationData.QPOS_PATTERN.value))
        if metadata_file.exists() and qpos_files:
            dataset_dirs.append(input_path)

    if not dataset_dirs:
        log_warning(f"No valid dataset directories found in: {input_path}")
        return

    log_info(f"Found {len(dataset_dirs)} dataset directories to process")

    # 确定工作进程数
    if args.workers is None:
        if args.parallel:
            num_workers = min(len(dataset_dirs), multiprocessing.cpu_count())
        else:
            num_workers = 1
    else:
        num_workers = min(args.workers, len(dataset_dirs))

    if args.parallel and num_workers > 1:
        log_info(f"Using parallel processing with {num_workers} workers")
        # 并行处理模式
        success_count = 0
        skipped_count = 0
        failed_count = 0

        # 准备任务参数
        tasks = []
        for dataset_dir in dataset_dirs:
            dataset_name = dataset_dir.name
            tasks.append((dataset_dir, output_path, config, args.skip_existing, dataset_name, args.format))

        # 使用进程池并行处理
        with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
            # 提交所有任务
            future_to_task = {executor.submit(process_dataset_wrapper, task): task for task in tasks}

            # 处理结果
            with tqdm(total=len(tasks), desc="Processing datasets") as pbar:
                for future in concurrent.futures.as_completed(future_to_task):
                    result = future.result()

                    if result["success"]:
                        if result.get("skipped", False):
                            skipped_count += 1
                            log_info(f"  ⚡ {result['message']}")
                        else:
                            success_count += 1
                            log_info(f"  ✓ {result['message']}")
                    else:
                        failed_count += 1
                        log_warning(f"  ✗ {result['message']}")

                    pbar.update(1)

        # 输出统计信息
        log_info("=" * 50)
        log_info("PARALLEL PROCESSING SUMMARY")
        log_info(f"  Total datasets: {len(dataset_dirs)}")
        log_info(f"  Successfully processed: {success_count}")
        log_info(f"  Skipped (already exist): {skipped_count}")
        log_info(f"  Failed: {failed_count}")
        log_info(f"  Workers used: {num_workers}")
        log_info("=" * 50)

    else:
        # 串行处理模式
        log_info("Using serial processing mode")
        success_count = 0
        skipped_count = 0

        for dataset_dir in tqdm(dataset_dirs, desc="Processing datasets"):
            dataset_name = dataset_dir.name
            output_file = output_path / f"{dataset_name}.hdf5"

            # 检查是否跳过已存在的文件
            if args.skip_existing and output_file.exists():
                log_info(f"Skipping existing file: {output_file}")
                skipped_count += 1
                continue

            success = process_single_dataset(dataset_dir, output_path, config, dataset_name, fmt=args.format)
            if success:
                success_count += 1

        log_info(
            f"Processing completed: {success_count}/{len(dataset_dirs)} datasets successfully processed, {skipped_count} skipped"
        )


if __name__ == "__main__":
    main()
