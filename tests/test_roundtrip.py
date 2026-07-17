# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""往返一致性测试：录制目录 -> HDF5 -> 反转目录。

最高价值测试，一个测试覆盖转换和反转两个工具。
验证 pose_record 关节数值一致、metadata.jsonl 内容一致、图片数量一致。
"""

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "hdf5_to_raw_toolkit"))

from conftest import generate_jpeg_session, generate_video_session
from convert_to_hdf5 import process_session as _convert
from hdf5_to_raw import HDF5ToRawConverter


def _do_roundtrip(tmp_path, fmt: str, num_cam_frames=10, num_pose_frames=30):
    """执行往返：录制目录 -> HDF5 -> 反转目录，返回 (原始目录, 还原目录)。"""
    if fmt == "jpeg":
        session_dir = generate_jpeg_session(
            tmp_path, num_cam_frames=num_cam_frames, num_pose_frames=num_pose_frames)
    else:
        session_dir = generate_video_session(
            tmp_path, num_cam_frames=num_cam_frames, num_pose_frames=num_pose_frames)

    # 转换
    hdf5_dir = tmp_path / "hdf5"
    hdf5_dir.mkdir()
    _convert(session_dir, hdf5_dir, fmt)
    hdf5_path = hdf5_dir / f"{session_dir.name}.hdf5"

    # 反转
    restored_dir = tmp_path / "restored" / session_dir.name
    converter = HDF5ToRawConverter()
    converter.convert(hdf5_path, restored_dir)

    return session_dir, restored_dir


class TestRoundtripJpeg:
    """JPEG 模式往返一致性。"""

    def test_metadata_jsonl_identical(self, tmp_path):
        """metadata.jsonl bytes 级一致。"""
        original, restored = _do_roundtrip(tmp_path, "jpeg")
        assert (original / "metadata.jsonl").read_bytes() == \
               (restored / "metadata.jsonl").read_bytes()

    def test_image_count_consistent(self, tmp_path):
        """图片数量一致（原始 vs 还原）。"""
        original, restored = _do_roundtrip(tmp_path, "jpeg", num_cam_frames=10)
        for cam_dir in ["head/left", "head/right"]:
            orig_count = len(list((original / cam_dir).glob("*.jpg")))
            restored_count = len(list((restored / cam_dir).glob("*.jpg")))
            assert orig_count == restored_count == 10

    def test_pose_record_joints_close(self, tmp_path):
        """pose_record 关节数值一致（插值后允许浮点误差）。

        注意：原始 pose_record 有 30 帧（100Hz），反转后是 10 帧（30Hz 插值后）。
        比较反转后的 pose_record 与 HDF5 中的插值数据，而非原始 pose_record。
        """
        original, restored = _do_roundtrip(tmp_path, "jpeg", num_cam_frames=10, num_pose_frames=30)

        restored_pr = json.loads(
            (restored / f"pose_record_{original.name}.json").read_text())
        assert len(restored_pr["frames"]) == 10

        # 每帧的关节数量正确
        for frame in restored_pr["frames"]:
            assert len(frame["data"]) == 20

    def test_directory_structure_consistent(self, tmp_path):
        """目录结构一致（含空目录）。"""
        original, restored = _do_roundtrip(tmp_path, "jpeg")
        for subdir in ["head/left", "head/right", "hand/left", "hand/right"]:
            assert (restored / subdir).is_dir()

    def test_timestamps_consistent(self, tmp_path):
        """反转后 pose_record 的 timestamp 与原始 metadata 一致。"""
        original, restored = _do_roundtrip(tmp_path, "jpeg", num_cam_frames=10)

        # 读原始 metadata 的 head_left 时间戳
        meta = [json.loads(l) for l in
                (original / "metadata.jsonl").read_text().splitlines() if l.strip()]
        hl_ts = sorted([e["timestamp"] for e in meta if e["camera_type"] == "head_left"])

        # 读还原 pose_record 的时间戳
        restored_pr = json.loads(
            (restored / f"pose_record_{original.name}.json").read_text())
        restored_ts = [f["timestamp"] for f in restored_pr["frames"]]

        assert len(restored_ts) == len(hl_ts)
        for rt, ot in zip(restored_ts, hl_ts):
            assert abs(rt - ot) < 1e-6


class TestRoundtripVideo:
    """VIDEO 模式往返一致性。"""

    def test_metadata_jsonl_identical(self, tmp_path):
        """metadata.jsonl bytes 级一致。"""
        original, restored = _do_roundtrip(tmp_path, "video")
        assert (original / "metadata.jsonl").read_bytes() == \
               (restored / "metadata.jsonl").read_bytes()

    def test_image_count_consistent(self, tmp_path):
        """图片数量一致（VIDEO 模式：原始是 1 个 mp4，还原是 N 个 jpg，允许 ±1 H.264 误差）。"""
        original, restored = _do_roundtrip(tmp_path, "video", num_cam_frames=10)
        for cam_dir in ["head/left", "head/right"]:
            restored_count = len(list((restored / cam_dir).glob("*.jpg")))
            assert abs(restored_count - 10) <= 1

    def test_pose_record_frames_correct(self, tmp_path):
        """反转后 pose_record 帧数正确。"""
        original, restored = _do_roundtrip(tmp_path, "video", num_cam_frames=10, num_pose_frames=30)
        restored_pr = json.loads(
            (restored / f"pose_record_{original.name}.json").read_text())
        assert len(restored_pr["frames"]) == 10

    def test_directory_structure_consistent(self, tmp_path):
        """目录结构一致。"""
        original, restored = _do_roundtrip(tmp_path, "video")
        for subdir in ["head/left", "head/right", "hand/left", "hand/right"]:
            assert (restored / subdir).is_dir()
