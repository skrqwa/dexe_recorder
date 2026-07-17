# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""端到端测试：hdf5_to_raw.py 反转工具。

验证从 HDF5 还原的目录结构、图片数量、pose_record、metadata.jsonl。
"""

import json
import os
import sys
from pathlib import Path

import h5py
import numpy as np
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from conftest import generate_jpeg_session, generate_video_session

# 将 hdf5_to_raw_toolkit 加入 path
TOOLKIT_DIR = SCRIPTS_DIR / "hdf5_to_raw_toolkit"
sys.path.insert(0, str(TOOLKIT_DIR))

from convert_to_hdf5 import process_session as _convert


def _convert_to_hdf5(session_dir: Path, output_dir: Path, fmt: str) -> Path:
    """转换 session 到 HDF5，返回 HDF5 文件路径。"""
    _convert(session_dir, output_dir, fmt)
    return output_dir / f"{session_dir.name}.hdf5"


class TestHdf5ToRawJpeg:
    """JPEG 模式反转测试。"""

    def test_jpeg_restore_images(self, tmp_path):
        """JPEG 模式：还原图片数量正确。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=10, num_pose_frames=30)
        hdf5_dir = tmp_path / "hdf5"
        hdf5_dir.mkdir()
        hdf5_path = _convert_to_hdf5(session_dir, hdf5_dir, "jpeg")

        # 反转
        from hdf5_to_raw import HDF5ToRawConverter
        converter = HDF5ToRawConverter()
        output_dir = tmp_path / "restored"
        n = converter.convert(hdf5_path, output_dir / session_dir.name)

        assert n == 10
        # 图片数量
        restored = output_dir / session_dir.name
        assert len(list((restored / "head/left").glob("*.jpg"))) == 10
        assert len(list((restored / "head/right").glob("*.jpg"))) == 10

    def test_jpeg_restore_metadata_jsonl(self, tmp_path):
        """JPEG 模式：metadata.jsonl bytes 级还原。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        hdf5_dir = tmp_path / "hdf5"
        hdf5_dir.mkdir()
        hdf5_path = _convert_to_hdf5(session_dir, hdf5_dir, "jpeg")

        from hdf5_to_raw import HDF5ToRawConverter
        converter = HDF5ToRawConverter()
        output_dir = tmp_path / "restored"
        converter.convert(hdf5_path, output_dir / session_dir.name)

        original = (session_dir / "metadata.jsonl").read_bytes()
        restored = (output_dir / session_dir.name / "metadata.jsonl").read_bytes()
        assert original == restored

    def test_jpeg_restore_pose_record(self, tmp_path):
        """JPEG 模式：pose_record 关节数据还原正确。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=10, num_pose_frames=30)
        hdf5_dir = tmp_path / "hdf5"
        hdf5_dir.mkdir()
        hdf5_path = _convert_to_hdf5(session_dir, hdf5_dir, "jpeg")

        from hdf5_to_raw import HDF5ToRawConverter
        converter = HDF5ToRawConverter()
        output_dir = tmp_path / "restored"
        converter.convert(hdf5_path, output_dir / session_dir.name)

        restored_pr = json.loads(
            (output_dir / session_dir.name / f"pose_record_{session_dir.name}.json").read_text())
        assert len(restored_pr["frames"]) == 10
        assert "ANKLE" in restored_pr["frames"][0]["data"]

    def test_jpeg_restore_empty_dirs(self, tmp_path):
        """JPEG 模式：空目录（hand/left 等）正确还原。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        hdf5_dir = tmp_path / "hdf5"
        hdf5_dir.mkdir()
        hdf5_path = _convert_to_hdf5(session_dir, hdf5_dir, "jpeg")

        from hdf5_to_raw import HDF5ToRawConverter
        converter = HDF5ToRawConverter()
        output_dir = tmp_path / "restored"
        converter.convert(hdf5_path, output_dir / session_dir.name)

        restored = output_dir / session_dir.name
        assert (restored / "hand/left").is_dir()
        assert (restored / "hand/right").is_dir()


class TestHdf5ToRawVideo:
    """VIDEO 模式反转测试。"""

    def test_video_restore_images(self, tmp_path):
        """VIDEO 模式：从 MP4 解码图片，数量正确（允许 ±1 帧 H.264 编码误差）。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=10, num_pose_frames=30)
        hdf5_dir = tmp_path / "hdf5"
        hdf5_dir.mkdir()
        hdf5_path = _convert_to_hdf5(session_dir, hdf5_dir, "video")

        from hdf5_to_raw import HDF5ToRawConverter
        converter = HDF5ToRawConverter()
        output_dir = tmp_path / "restored"
        n = converter.convert(hdf5_path, output_dir / session_dir.name)

        assert n == 10
        restored = output_dir / session_dir.name
        # H.264 编码可能丢最后一帧，允许 ±1
        left_count = len(list((restored / "head/left").glob("*.jpg")))
        right_count = len(list((restored / "head/right").glob("*.jpg")))
        assert abs(left_count - 10) <= 1
        assert abs(right_count - 10) <= 1

    def test_video_restore_metadata_jsonl(self, tmp_path):
        """VIDEO 模式：metadata.jsonl bytes 级还原。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        hdf5_dir = tmp_path / "hdf5"
        hdf5_dir.mkdir()
        hdf5_path = _convert_to_hdf5(session_dir, hdf5_dir, "video")

        from hdf5_to_raw import HDF5ToRawConverter
        converter = HDF5ToRawConverter()
        output_dir = tmp_path / "restored"
        converter.convert(hdf5_path, output_dir / session_dir.name)

        original = (session_dir / "metadata.jsonl").read_bytes()
        restored = (output_dir / session_dir.name / "metadata.jsonl").read_bytes()
        assert original == restored


class TestHdf5ToRawBatch:
    """批量处理测试。"""

    def test_batch_convert(self, tmp_path):
        """批量反转多个 HDF5 文件。"""
        # 生成两个 session
        s1 = generate_jpeg_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        s2 = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)

        hdf5_dir = tmp_path / "hdf5"
        hdf5_dir.mkdir()
        _convert(s1, hdf5_dir, "jpeg")
        _convert(s2, hdf5_dir, "video")

        from hdf5_to_raw import convert_batch
        output_dir = tmp_path / "restored"
        success, total = convert_batch(hdf5_dir, output_dir)

        assert success == 2
        assert total == 10  # 5 + 5
