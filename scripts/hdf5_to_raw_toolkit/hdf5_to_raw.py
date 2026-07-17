# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""hdf5_to_raw.py - 将 HDF5 文件还原为原始录制数据结构（图片 + pose_record JSON）。

从遥操 scripts/tools/hdf5_to_raw_toolkit/ 移植，适配 dexe_recorder 的 HDF5 格式。

用法:
    python hdf5_to_raw.py --input <hdf5_file> --output <output_dir>
    python hdf5_to_raw.py --input data/hdf5/test2/xxx.hdf5 --output data/restored/test2/xxx/

可批量:
    python hdf5_to_raw.py --input-dir data/hdf5 --output-dir data/restored

依赖: python3, h5py, numpy, Pillow, av(PyAV)
    pip install h5py numpy Pillow av
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import h5py
import numpy as np

# 复用同目录下的 image_video_encode.py
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from image_video_encode import decode_mp4_bytes_to_rgb_images  # noqa: E402


# ── 对齐策略（可替换/扩展） ──────────────────────────────────────

@dataclass
class AlignedFrame:
    """一帧对齐后的数据。"""
    frame_index: int
    timestamp_ns: int
    images: Dict[str, Any]  # camera_name -> PIL Image
    joints: Dict[str, float]  # joint_name -> value


class AlignmentStrategy(ABC):
    """对齐策略基类。子类实现具体的对齐/还原方式。

    当前的 HDF5 是转换器对齐+插值后的产物，joints timestamps 和 camera timestamps
    已统一（同一帧共用一个时间戳）。未来如果转换器改为保留原始数据，可新增策略。
    """

    @abstractmethod
    def align(
        self,
        h5_file: h5py.File,
    ) -> List[AlignedFrame]:
        """从 HDF5 文件中提取对齐后的帧列表。"""
        ...


class DefaultAlignmentStrategy(AlignmentStrategy):
    """默认对齐策略：直接按帧序号对齐（HDF5 已对齐过）。

    HDF5 中各 group 的 frames 属性一致，timestamps 也一致，
    直接按帧索引取数据即可。
    """

    def align(
        self,
        h5_file: h5py.File,
    ) -> List[AlignedFrame]:
        """从 HDF5 文件中提取对齐后的帧列表。

        注意：此方法会一次性加载所有帧到内存，大文件可能 OOM。
        对于大文件建议用 HDF5ToRawConverter.convert() 直接逐帧写入。
        """
        converter = HDF5ToRawConverter()
        return converter._extract_frames(h5_file)


# ── HDF5 反转器 ──────────────────────────────────────────────────

class HDF5ToRawConverter:
    """将 HDF5 文件还原为原始录制数据结构。"""

    # HDF5 camera group name -> 原始目录路径映射
    CAMERA_DIR_MAP = {
        "camera_head_left": "head/left",
        "camera_head_right": "head/right",
        "camera_hand_left": "hand/left",
        "camera_hand_right": "hand/right",
    }

    def __init__(self, strategy: Optional[AlignmentStrategy] = None) -> None:
        self._strategy = strategy or DefaultAlignmentStrategy()

    def convert(self, hdf5_path: Path, output_dir: Path) -> int:
        """转换单个 HDF5 文件到输出目录（逐相机逐帧写入，避免 OOM）。

        Args:
            hdf5_path: HDF5 文件路径
            output_dir: 输出目录（session 目录）

        Returns:
            还原的帧数
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        with h5py.File(hdf5_path, "r") as f:
            frames_count = int(f.attrs.get("frames", 0))
            if frames_count == 0:
                if "joints" in f and "data" in f["joints"]:
                    frames_count = f["joints"]["data"].shape[0]
                else:
                    for name in f.keys():
                        g = f[name]
                        if isinstance(g, h5py.Group) and "timestamps" in g:
                            frames_count = g["timestamps"].shape[0]
                            break
            if frames_count == 0:
                print(f"  [WARN] 无有效帧: {hdf5_path}")
                return 0

            # 收集 camera groups 信息
            camera_groups: Dict[str, h5py.Group] = {}
            for name in f.keys():
                g = f[name]
                if isinstance(g, h5py.Group):
                    encoding = g.attrs.get("encoding", "")
                    if encoding in ("mp4", "jpeg"):
                        camera_groups[name] = g

            # 创建相机目录（从 metadata.jsonl 读取所有相机路径，包括无数据的）
            cam_dirs: Dict[str, Path] = {}
            for cam_name in camera_groups:
                dir_name = self.CAMERA_DIR_MAP.get(cam_name, cam_name)
                cam_dir = output_dir / dir_name
                cam_dir.mkdir(parents=True, exist_ok=True)
                cam_dirs[cam_name] = cam_dir

            # 从 metadata.jsonl 补充创建所有相机的目录（原始数据可能有空目录）
            if "metadata_jsonl" in f:
                meta_bytes = bytes(f["metadata_jsonl"][()])
                if meta_bytes:
                    seen_dirs = set()
                    for line in meta_bytes.decode("utf-8", errors="ignore").splitlines():
                        try:
                            entry = json.loads(line)
                            img_path = entry.get("image_path", "")
                            if img_path:
                                cam_dir_name = str(Path(img_path).parent)
                                if cam_dir_name not in seen_dirs:
                                    seen_dirs.add(cam_dir_name)
                                    (output_dir / cam_dir_name).mkdir(parents=True, exist_ok=True)
                        except (json.JSONDecodeError, KeyError):
                            pass

            # 从 subdirs attrs 创建所有原始子目录（包括空目录如 hand/left）
            subdirs_attr = f.attrs.get("subdirs", "[]")
            if isinstance(subdirs_attr, bytes):
                subdirs_attr = subdirs_attr.decode()
            for subdir in json.loads(subdirs_attr):
                (output_dir / subdir).mkdir(parents=True, exist_ok=True)

            # 先收集 joints 数据和基准时间戳（不依赖图片解码）
            joints_data: Optional[np.ndarray] = None
            joints_columns: List[str] = []
            if "joints" in f and "data" in f["joints"]:
                joints_group = f["joints"]
                joints_data = joints_group["data"][:]
                cols = joints_group.attrs.get("columns", "[]")
                if isinstance(cols, bytes):
                    cols = cols.decode()
                joints_columns = json.loads(cols)

            # 取基准时间戳
            ref_ts = None
            for cam_group in camera_groups.values():
                ref_ts = cam_group["timestamps"][:]
                break
            if ref_ts is None and "joints" in f and "timestamps" in f["joints"]:
                ref_ts = f["joints"]["timestamps"][:]

            # 先写辅助文件（不依赖图片解码，避免 OOM 时也能还原）
            self._write_pose_record(f, frames_count, joints_data, joints_columns, ref_ts, hdf5_path, output_dir)
            self._write_raw_files(f, output_dir)

            # 逐相机解码视频并写入图片（避免一次性全部解码到内存）
            for cam_name, cam_group in camera_groups.items():
                data_ds = cam_group["data"]
                encoding = cam_group.attrs.get("encoding", "")
                cam_dir = cam_dirs[cam_name]

                if encoding == "mp4":
                    # 视频模式：解码整个 MP4，逐帧保存
                    mp4_bytes = bytes(data_ds[()])
                    images = decode_mp4_bytes_to_rgb_images(mp4_bytes)
                    for i, img in enumerate(images):
                        img.save(cam_dir / f"{i:06d}.jpg", "JPEG", quality=95)
                    del images  # 释放内存
                elif encoding == "jpeg":
                    # JPEG 模式：逐帧解码
                    from PIL import Image
                    import io as _io
                    for i in range(data_ds.shape[0]):
                        jpg_bytes = bytes(data_ds[i])
                        img = Image.open(_io.BytesIO(jpg_bytes)).convert("RGB")
                        img.save(cam_dir / f"{i:06d}.jpg", "JPEG", quality=95)

        return frames_count

    def _extract_frames(self, h5_file: h5py.File) -> List[AlignedFrame]:
        """从 HDF5 提取帧列表（内部方法，strategy 调用）。"""
        frames_count = int(h5_file.attrs.get("frames", 0))
        if frames_count == 0:
            if "joints" in h5_file and "data" in h5_file["joints"]:
                frames_count = h5_file["joints"]["data"].shape[0]
            else:
                for name in h5_file.keys():
                    g = h5_file[name]
                    if isinstance(g, h5py.Group) and "timestamps" in g:
                        frames_count = g["timestamps"].shape[0]
                        break
        if frames_count == 0:
            return []

        # 收集 camera groups
        camera_groups: Dict[str, h5py.Group] = {}
        for name in h5_file.keys():
            g = h5_file[name]
            if isinstance(g, h5py.Group):
                encoding = g.attrs.get("encoding", "")
                if encoding in ("mp4", "jpeg"):
                    camera_groups[name] = g

        # 解码视频
        camera_images: Dict[str, List] = {}
        for cam_name, cam_group in camera_groups.items():
            data_ds = cam_group["data"]
            encoding = cam_group.attrs.get("encoding", "")
            if encoding == "mp4":
                mp4_bytes = bytes(data_ds[()])
                camera_images[cam_name] = decode_mp4_bytes_to_rgb_images(mp4_bytes)
            elif encoding == "jpeg":
                from PIL import Image
                import io as _io
                imgs = []
                for i in range(data_ds.shape[0]):
                    jpg_bytes = bytes(data_ds[i])
                    imgs.append(Image.open(_io.BytesIO(jpg_bytes)).convert("RGB"))
                camera_images[cam_name] = imgs
            else:
                camera_images[cam_name] = []

        # joints
        joints_data: Optional[np.ndarray] = None
        joints_columns: List[str] = []
        if "joints" in h5_file and "data" in h5_file["joints"]:
            joints_group = h5_file["joints"]
            joints_data = joints_group["data"][:]
            cols = joints_group.attrs.get("columns", "[]")
            if isinstance(cols, bytes):
                cols = cols.decode()
            joints_columns = json.loads(cols)

        # 基准时间戳
        ref_ts = None
        for cam_group in camera_groups.values():
            ref_ts = cam_group["timestamps"][:]
            break
        if ref_ts is None and "joints" in h5_file and "timestamps" in h5_file["joints"]:
            ref_ts = h5_file["joints"]["timestamps"][:]

        result: List[AlignedFrame] = []
        for i in range(frames_count):
            ts_ns = int(ref_ts[i]) if ref_ts is not None and i < len(ref_ts) else 0
            images: Dict[str, Any] = {}
            for cam_name, images_list in camera_images.items():
                if i < len(images_list):
                    images[cam_name] = images_list[i]
            joints: Dict[str, float] = {}
            if joints_data is not None and i < len(joints_data):
                for j, col in enumerate(joints_columns):
                    joints[col] = float(joints_data[i][j])
            result.append(AlignedFrame(frame_index=i, timestamp_ns=ts_ns, images=images, joints=joints))
        return result

    def _write_pose_record(
        self,
        h5_file: h5py.File,
        frames_count: int,
        joints_data: Optional[np.ndarray],
        joints_columns: List[str],
        ref_ts: Optional[np.ndarray],
        hdf5_path: Path,
        output_dir: Path,
    ) -> None:
        """写 pose_record JSON 文件，尽量还原原始格式。"""
        session_id = hdf5_path.stem

        # 构造 pose_record frames
        pose_frames = []
        for i in range(frames_count):
            ts_sec = float(ref_ts[i]) / 1e9 if ref_ts is not None and i < len(ref_ts) else 0.0
            joints: Dict[str, float] = {}
            if joints_data is not None and i < len(joints_data):
                for j, col in enumerate(joints_columns):
                    joints[col] = float(joints_data[i][j])
            pose_frames.append({
                "frame_id": i,
                "timestamp": ts_sec,
                "data": joints,
            })

        if pose_frames:
            start_time = pose_frames[0]["timestamp"]
            end_time = pose_frames[-1]["timestamp"]
            duration = end_time - start_time
        else:
            start_time = 0.0
            end_time = 0.0
            duration = 0.0

        record = {
            "session_id": session_id,
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "frame_count": len(pose_frames),
            "metadata": {},
            "frames": pose_frames,
        }

        # 恢复额外 attrs
        if "language" in h5_file.attrs:
            lang = h5_file.attrs["language"]
            if isinstance(lang, bytes):
                lang = lang.decode()
            record["language"] = lang
        if "robot_type" in h5_file.attrs:
            robot_type = h5_file.attrs["robot_type"]
            if isinstance(robot_type, bytes):
                robot_type = robot_type.decode()
            record["robot_type"] = robot_type

        pose_path = output_dir / f"pose_record_{session_id}.json"
        with open(pose_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

    def _write_raw_files(self, h5_file: h5py.File, output_dir: Path) -> None:
        """还原原始辅助文件（metadata.jsonl、tactile.jsonl）。"""
        # metadata.jsonl
        if "metadata_jsonl" in h5_file:
            data = bytes(h5_file["metadata_jsonl"][()])
            with open(output_dir / "metadata.jsonl", "wb") as f:
                f.write(data)

        # tactile.jsonl（可能不存在）
        if "tactile_jsonl" in h5_file:
            data = bytes(h5_file["tactile_jsonl"][()])
            with open(output_dir / "tactile.jsonl", "wb") as f:
                f.write(data)


# ── 批量处理 ──────────────────────────────────────────────────────

def find_hdf5_files(input_path: Path) -> List[Path]:
    """查找 HDF5 文件：单个文件或目录下所有 .hdf5。"""
    if input_path.is_file() and input_path.suffix in (".hdf5", ".h5"):
        return [input_path]
    if input_path.is_dir():
        return sorted(input_path.rglob("*.hdf5"))
    return []


def convert_batch(
    input_path: Path,
    output_dir: Path,
    strategy: Optional[AlignmentStrategy] = None,
) -> Tuple[int, int]:
    """批量转换 HDF5 文件。

    Args:
        input_path: 单个 HDF5 文件或包含 HDF5 的目录
        output_dir: 输出根目录
        strategy: 对齐策略（None 用默认）

    Returns:
        (成功数, 总帧数)
    """
    converter = HDF5ToRawConverter(strategy=strategy)
    hdf5_files = find_hdf5_files(input_path)

    if not hdf5_files:
        print(f"未找到 HDF5 文件: {input_path}")
        return 0, 0

    print(f"找到 {len(hdf5_files)} 个 HDF5 文件")

    success_count = 0
    total_frames = 0

    for hdf5_file in hdf5_files:
        print(f"\n转换: {hdf5_file}")
        # 保持目录结构
        if input_path.is_dir():
            rel = hdf5_file.relative_to(input_path)
            # 去掉 .hdf5 后缀作为 session 目录名
            out = output_dir / rel.with_suffix("")
        else:
            # 单文件：用文件名（去后缀）作为输出目录名
            out = output_dir / hdf5_file.stem

        try:
            n = converter.convert(hdf5_file, out)
            print(f"  还原 {n} 帧到 {out}")
            success_count += 1
            total_frames += n
        except Exception as e:
            print(f"  [ERROR] {e}")

    print(f"\n完成: {success_count}/{len(hdf5_files)} 文件, 共 {total_frames} 帧")
    return success_count, total_frames


# ── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="将 HDF5 文件还原为原始录制数据结构（图片 + pose_record JSON）")
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        help="单个 HDF5 文件路径",
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        default=None,
        help="包含 HDF5 文件的目录（批量处理）",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="输出目录",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="default",
        choices=["default"],
        help="对齐策略（当前仅 default，预留扩展）",
    )
    args = parser.parse_args()

    # 确定输入
    input_path = None
    if args.input:
        input_path = Path(args.input)
    elif args.input_dir:
        input_path = Path(args.input_dir)
    else:
        parser.error("必须指定 --input 或 --input-dir")

    if not input_path.exists():
        print(f"输入路径不存在: {input_path}")
        sys.exit(1)

    output_dir = Path(args.output)

    # 选择策略（预留扩展点）
    strategy = DefaultAlignmentStrategy()
    if args.strategy == "default":
        strategy = DefaultAlignmentStrategy()
    # 未来可扩展: elif args.strategy == "raw_preserve": RawPreserveStrategy()

    convert_batch(input_path, output_dir, strategy=strategy)


if __name__ == "__main__":
    main()
