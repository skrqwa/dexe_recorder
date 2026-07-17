# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""测试 fixture：生成小型录制数据用于端到端测试。

生成一个最小化的 session 目录，包含：
- metadata.jsonl（2 个相机，每相机 N 帧）
- pose_record_*.json（M 帧关节数据，M > N 以测试插值）
- head/left/000000.jpg ... (JPEG 模式) 或 head/left/video.mp4 (VIDEO 模式)
- head/right/...
- hand/left/（空目录）
- hand/right/（空目录）
"""

import json
import os
import struct
from pathlib import Path
from typing import Tuple

import numpy as np
from PIL import Image


JOINT_KEYS = [
    "ANKLE", "KNEE", "BUTTOCK", "WAIST", "NECK1", "NECK2",
    "LEFT_J1", "LEFT_J2", "LEFT_J3", "LEFT_J4", "LEFT_J5", "LEFT_J6", "LEFT_J7",
    "RIGHT_J1", "RIGHT_J2", "RIGHT_J3", "RIGHT_J4", "RIGHT_J5", "RIGHT_J6",
    "RIGHT_J7",
]

CAMERAS = ["head_left", "head_right"]


def _make_jpeg_image(width: int = 64, height: int = 48, color=(128, 128, 128)) -> bytes:
    """生成一个小型 JPEG 图片字节。"""
    img = Image.new("RGB", (width, height), color)
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _make_mp4_bytes(num_frames: int, width: int = 64, height: int = 48) -> bytes:
    """生成一个小型 MP4 视频字节流（用 PyAV 编码）。"""
    import av
    from io import BytesIO

    buf = BytesIO()
    container = av.open(buf, "w", format="mp4")
    stream = container.add_stream("libx264", rate=30)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"

    for i in range(num_frames):
        img = Image.new("RGB", (width, height), (128 + i % 64, 128, 128))
        frame = av.VideoFrame.from_image(img)
        for packet in stream.encode(frame):
            container.mux(packet)

    for packet in stream.encode():
        container.mux(packet)

    container.close()
    return buf.getvalue()


def generate_jpeg_session(
    output_dir: Path,
    num_cam_frames: int = 10,
    num_pose_frames: int = 30,
    image_size: Tuple[int, int] = (64, 48),
) -> Path:
    """生成 JPEG 模式的录制 session 目录。

    Args:
        output_dir: 输出根目录
        num_cam_frames: 每个相机的帧数
        num_pose_frames: pose_record 的帧数（>num_cam_frames 以测试插值）
        image_size: (width, height)

    Returns:
        session 目录路径
    """
    session_id = "test_session_jpeg"
    session_dir = output_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    width, height = image_size
    base_ts = 1000000.0  # 基准时间戳
    cam_interval = 1.0 / 30.0  # 30Hz
    pose_interval = 1.0 / 100.0  # 100Hz（模拟旧版）

    # 创建相机目录和图片
    cam_dirs = {"head_left": "head/left", "head_right": "head/right"}
    for cam_dir_rel in cam_dirs.values():
        (session_dir / cam_dir_rel).mkdir(parents=True, exist_ok=True)
    # 空目录
    (session_dir / "hand/left").mkdir(parents=True, exist_ok=True)
    (session_dir / "hand/right").mkdir(parents=True, exist_ok=True)

    # 写 metadata.jsonl 和图片
    metadata_lines = []
    for frame_id in range(num_cam_frames):
        ts = base_ts + frame_id * cam_interval
        for cam_type in CAMERAS:
            image_path = f"{cam_dirs[cam_type]}/{frame_id:06d}.jpg"
            # 写图片
            jpg_bytes = _make_jpeg_image(width, height, (frame_id * 10 % 256, 128, 128))
            (session_dir / image_path).write_bytes(jpg_bytes)
            # 写 metadata 行
            entry = {
                "timestamp": ts,
                "frame_id": frame_id,
                "camera_type": cam_type,
                "ros_timestamp": ts,
                "image_path": image_path,
            }
            metadata_lines.append(json.dumps(entry))

    (session_dir / "metadata.jsonl").write_text("\n".join(metadata_lines) + "\n")

    # 写 pose_record
    pose_frames = []
    for i in range(num_pose_frames):
        ts = base_ts + i * pose_interval
        data = {}
        for j, key in enumerate(JOINT_KEYS):
            data[key] = float(i * 0.01 + j * 0.1)
        pose_frames.append({"frame_id": i, "timestamp": ts, "data": data})

    pose_record = {
        "session_id": session_id,
        "start_time": pose_frames[0]["timestamp"],
        "end_time": pose_frames[-1]["timestamp"],
        "duration": pose_frames[-1]["timestamp"] - pose_frames[0]["timestamp"],
        "frame_count": len(pose_frames),
        "metadata": {},
        "frames": pose_frames,
    }
    (session_dir / f"pose_record_{session_id}.json").write_text(
        json.dumps(pose_record, ensure_ascii=False))

    return session_dir


def generate_video_session(
    output_dir: Path,
    num_cam_frames: int = 10,
    num_pose_frames: int = 30,
    image_size: Tuple[int, int] = (64, 48),
) -> Path:
    """生成 VIDEO 模式的录制 session 目录。

    与 JPEG 模式类似，但每个相机目录下是 video.mp4 而非 jpg 序列。
    metadata.jsonl 的 image_path 仍写 000000.jpg（虚拟路径，与实际录制一致）。

    Args:
        output_dir: 输出根目录
        num_cam_frames: 每个相机的帧数
        num_pose_frames: pose_record 的帧数
        image_size: (width, height)

    Returns:
        session 目录路径
    """
    session_id = "test_session_video"
    session_dir = output_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    width, height = image_size
    base_ts = 1000000.0
    cam_interval = 1.0 / 30.0
    pose_interval = 1.0 / 100.0

    cam_dirs = {"head_left": "head/left", "head_right": "head/right"}
    for cam_dir_rel in cam_dirs.values():
        (session_dir / cam_dir_rel).mkdir(parents=True, exist_ok=True)
    (session_dir / "hand/left").mkdir(parents=True, exist_ok=True)
    (session_dir / "hand/right").mkdir(parents=True, exist_ok=True)

    # 写 metadata.jsonl（image_path 写虚拟 jpg 名）和 video.mp4
    metadata_lines = []
    for cam_type in CAMERAS:
        cam_dir_rel = cam_dirs[cam_type]
        # 生成 video.mp4
        mp4_bytes = _make_mp4_bytes(num_cam_frames, width, height)
        (session_dir / cam_dir_rel / "video.mp4").write_bytes(mp4_bytes)

    for frame_id in range(num_cam_frames):
        ts = base_ts + frame_id * cam_interval
        for cam_type in CAMERAS:
            image_path = f"{cam_dirs[cam_type]}/{frame_id:06d}.jpg"
            entry = {
                "timestamp": ts,
                "frame_id": frame_id,
                "camera_type": cam_type,
                "ros_timestamp": ts,
                "image_path": image_path,
            }
            metadata_lines.append(json.dumps(entry))

    (session_dir / "metadata.jsonl").write_text("\n".join(metadata_lines) + "\n")

    # 写 pose_record
    pose_frames = []
    for i in range(num_pose_frames):
        ts = base_ts + i * pose_interval
        data = {}
        for j, key in enumerate(JOINT_KEYS):
            data[key] = float(i * 0.01 + j * 0.1)
        pose_frames.append({"frame_id": i, "timestamp": ts, "data": data})

    pose_record = {
        "session_id": session_id,
        "start_time": pose_frames[0]["timestamp"],
        "end_time": pose_frames[-1]["timestamp"],
        "duration": pose_frames[-1]["timestamp"] - pose_frames[0]["timestamp"],
        "frame_count": len(pose_frames),
        "metadata": {},
        "frames": pose_frames,
    }
    (session_dir / f"pose_record_{session_id}.json").write_text(
        json.dumps(pose_record, ensure_ascii=False))

    return session_dir
