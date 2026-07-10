#!/usr/bin/env python3
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
  h5py, numpy, Pillow（JPEG 模式需要读图片尺寸）, tqdm（可选，进度条）

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
import numpy as np
from pathlib import Path
from typing import List, Dict, Optional, Tuple

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
    with open(meta_path, "r") as f:
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
    with open(json_files[0], "r") as f:
        return json.load(f)


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


def get_video_info(video_path: Path) -> Optional[Tuple[int, int]]:
    """用 gst-discoverer 获取视频宽高（不依赖 cv2）"""
    import subprocess
    try:
        result = subprocess.run(
            ["gst-discoverer-1.0", str(video_path)],
            capture_output=True, text=True, timeout=10
        )
        width = height = None
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("Width:"):
                width = int(line.split(":")[1].strip())
            elif line.startswith("Height:"):
                height = int(line.split(":")[1].strip())
        if width and height:
            return (height, width)
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


def process_session(data_dir: Path, output_dir: Path, fmt: str) -> bool:
    """处理单个 session，生成 HDF5"""
    session_id = data_dir.name

    # 加载数据
    metadata = load_metadata(data_dir)
    if not metadata:
        print(f"  [SKIP] {session_id}: 无 metadata.jsonl")
        return False

    pose_record = load_pose_record(data_dir)
    if not pose_record:
        print(f"  [SKIP] {session_id}: 无 pose_record_*.json")
        return False

    # 按相机分组
    camera_groups = group_metadata_by_camera(metadata)

    # 自动检测格式（如果未指定）
    if fmt == "auto":
        fmt = detect_format(data_dir, camera_groups)
        print(f"  检测到格式: {fmt}")

    # 帧数：以关节数据为准
    pose_frames = pose_record.get("frames", [])
    num_frames = len(pose_frames)
    if num_frames == 0:
        print(f"  [SKIP] {session_id}: 无关节数据")
        return False

    # 关节键名
    joint_keys = []
    if pose_frames:
        joint_keys = list(pose_frames[0].get("data", {}).keys())

    # 相机列表
    selected_cameras = sorted(camera_groups.keys())
    if not selected_cameras:
        print(f"  [SKIP] {session_id}: 无相机数据")
        return False

    # 时间戳：用关节帧的时间戳
    timestamps = [f.get("timestamp", 0.0) for f in pose_frames]

    # 创建 HDF5
    hdf5_path = output_dir / f"{session_id}.hdf5"
    print(f"  创建 HDF5: {hdf5_path}")

    with h5py.File(hdf5_path, "w") as h5:
        # 全局属性
        h5.attrs["alignment_method"] = "nearest_timestamp"
        h5.attrs["cameras"] = json.dumps(selected_cameras)
        h5.attrs["description"] = "Auto mode recording from dexe_recorder"
        h5.attrs["frames"] = num_frames
        h5.attrs["language"] = ""
        h5.attrs["robot_type"] = "W1_Pro"
        h5.attrs["sample_rate"] = 30.0

        # === 相机数据 ===
        for cam_type in selected_cameras:
            cam_entries = camera_groups[cam_type]
            hdf5_name = f"camera_{cam_type}"
            cam_group = h5.create_group(hdf5_name)
            cam_group.attrs["type"] = "image"

            # 获取图像尺寸
            shape = None
            if fmt == "jpeg":
                # 读第一张图片获取尺寸
                first_path = cam_entries[0].get("image_path", "")
                shape = get_image_shape(data_dir, first_path)
            else:
                # video 模式：从 mp4 获取尺寸
                first_path = cam_entries[0].get("image_path", "")
                cam_dir = Path(first_path).parent
                video_path = data_dir / cam_dir / "video.mp4"
                if video_path.exists():
                    wh = get_video_info(video_path)
                    if wh:
                        shape = (wh[0], wh[1], 3)

            if shape is None:
                shape = (1080, 1920, 3)  # 默认值
                print(f"  [WARN] {cam_type}: 无法获取尺寸，用默认 {shape}")

            height, width, channels = shape
            cam_group.attrs["height"] = height
            cam_group.attrs["width"] = width
            cam_group.attrs["channels"] = channels
            cam_group.attrs["frames"] = len(cam_entries)
            cam_group.attrs["sample_rate"] = 30.0

            # 时间戳
            cam_ts = [e.get("timestamp", 0.0) for e in cam_entries]
            cam_ts_ns = np.array([unix_to_uint64_ns(ts) for ts in cam_ts], dtype=np.uint64)
            cam_group.create_dataset("timestamps", data=cam_ts_ns, compression="gzip", compression_opts=4)

            if fmt == "video":
                # VIDEO 模式：读整个 mp4 文件作为单个 varlen blob
                cam_group.attrs["encoding"] = "mp4"
                first_path = cam_entries[0].get("image_path", "")
                cam_dir = Path(first_path).parent
                video_path = data_dir / cam_dir / "video.mp4"

                dt = h5py.vlen_dtype(np.dtype("uint8"))
                data_ds = cam_group.create_dataset("data", shape=(), dtype=dt)

                if video_path.exists():
                    mp4_bytes = video_path.read_bytes()
                    data_ds[()] = np.frombuffer(mp4_bytes, dtype=np.uint8)
                    print(f"  {cam_type}: {len(mp4_bytes)} bytes MP4")
                else:
                    print(f"  [WARN] {cam_type}: video.mp4 不存在: {video_path}")

            else:
                # JPEG 模式：逐帧读取 jpg 文件
                cam_group.attrs["encoding"] = "jpeg"
                dt = h5py.vlen_dtype(np.dtype("uint8"))
                data_ds = cam_group.create_dataset(
                    "data", shape=(len(cam_entries),), dtype=dt,
                    compression="gzip", compression_opts=4
                )

                for i, entry in enumerate(tqdm(cam_entries, desc=f"  {cam_type}")):
                    image_path = entry.get("image_path", "")
                    full_path = data_dir / image_path
                    if full_path.exists():
                        data_ds[i] = np.frombuffer(full_path.read_bytes(), dtype=np.uint8)
                    else:
                        data_ds[i] = np.array([], dtype=np.uint8)

        # === 关节数据 ===
        joints_group = h5.create_group("joints")
        joints_group.attrs["type"] = "vector"
        joints_group.attrs["frames"] = num_frames
        joints_group.attrs["sample_rate"] = 30.0
        joints_group.attrs["columns"] = json.dumps(joint_keys)

        # 关节数据矩阵
        qpos = np.zeros((num_frames, len(joint_keys)), dtype=np.float32)
        for i, frame in enumerate(pose_frames):
            data = frame.get("data", {})
            for j, key in enumerate(joint_keys):
                qpos[i, j] = data.get(key, 0.0)
        joints_group.create_dataset("data", data=qpos, compression="gzip", compression_opts=4)

        # 时间戳
        ts_ns = np.array([unix_to_uint64_ns(ts) for ts in timestamps], dtype=np.uint64)
        joints_group.create_dataset("timestamps", data=ts_ns, compression="gzip", compression_opts=4)

    file_size = hdf5_path.stat().st_size / (1024 * 1024)
    print(f"  ✓ {session_id}: {num_frames} 帧, {len(selected_cameras)} 相机, {file_size:.1f}MB")
    return True


def main():
    parser = argparse.ArgumentParser(description="dexe_recorder 录制数据转 HDF5")
    parser.add_argument("--input", required=True, help="录制数据目录（含多个 session 子目录，或单个 session 目录）")
    parser.add_argument("--output", required=True, help="HDF5 输出目录")
    parser.add_argument("--format", default="auto", choices=["auto", "video", "jpeg"],
                        help="录制格式：auto（自动检测）、video、jpeg（默认 auto）")
    parser.add_argument("--skip-existing", action="store_true", help="跳过已存在的 HDF5 文件")
    args = parser.parse_args()

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

    for session_dir in sessions:
        session_id = session_dir.name
        hdf5_path = output_dir / f"{session_id}.hdf5"

        if args.skip_existing and hdf5_path.exists():
            print(f"[SKIP] {session_id}: HDF5 已存在")
            skipped += 1
            continue

        print(f"\n处理: {session_id}")
        if process_session(session_dir, output_dir, args.format):
            success += 1

    print(f"\n完成: {success}/{len(sessions)} 成功, {skipped} 跳过")
    if success > 0:
        print(f"HDF5 文件在: {output_dir}")


if __name__ == "__main__":
    main()
