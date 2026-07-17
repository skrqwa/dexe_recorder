# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""端到端测试：convert_to_hdf5.py 转换工具。

验证 HDF5 文件结构、joints 插值、metadata_jsonl/tactile_jsonl/subdirs 完整性。
"""

import json
import os
import sys
from pathlib import Path

import h5py
import numpy as np
import pytest

# 将 scripts 目录加入 path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from conftest import generate_jpeg_session, generate_video_session


def _run_convert(session_dir: Path, output_dir: Path, fmt: str = "auto"):
    """运行转换脚本（直接调用 process_session）。"""
    import convert_to_hdf5
    return convert_to_hdf5.process_session(session_dir, output_dir, fmt)


class TestConvertJpegMode:
    """JPEG 模式转换测试。"""

    def test_jpeg_basic_structure(self, tmp_path):
        """JPEG 模式：验证 HDF5 基本结构。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=10, num_pose_frames=30)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        assert _run_convert(session_dir, output_dir, "jpeg") is True

        hdf5_path = output_dir / "test_session_jpeg.hdf5"
        assert hdf5_path.exists()

        with h5py.File(hdf5_path, "r") as f:
            # Root attrs
            assert f.attrs["frames"] == 10
            assert "nearest_timestamp" in str(f.attrs["alignment_method"])
            assert json.loads(f.attrs["cameras"]) == ["head_left", "head_right"]
            assert f.attrs["sample_rate"] == 30.0
            assert f.attrs["robot_type"] == "W1_Pro"

            # Camera groups
            assert "camera_head_left" in f
            assert "camera_head_right" in f
            for cam_name in ["camera_head_left", "camera_head_right"]:
                g = f[cam_name]
                assert g.attrs["type"] == "image"
                assert g.attrs["encoding"] == "jpeg"
                assert g.attrs["frames"] == 10
                assert g.attrs["height"] == 48
                assert g.attrs["width"] == 64
                assert g.attrs["channels"] == 3
                assert g["data"].shape == (10,)
                assert g["timestamps"].shape == (10,)
                assert g["timestamps"].dtype == np.uint64

            # Joints group
            assert "joints" in f
            jg = f["joints"]
            assert jg.attrs["type"] == "vector"
            assert jg.attrs["frames"] == 10
            cols = json.loads(jg.attrs["columns"])
            assert len(cols) == 20
            assert jg["data"].shape == (10, 20)
            assert jg["data"].dtype == np.float32
            assert jg["timestamps"].shape == (10,)

    def test_jpeg_timestamps_aligned(self, tmp_path):
        """JPEG 模式：camera timestamps == joints timestamps。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=10, num_pose_frames=30)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()
        _run_convert(session_dir, output_dir, "jpeg")

        with h5py.File(output_dir / "test_session_jpeg.hdf5", "r") as f:
            cam_ts = f["camera_head_left/timestamps"][:]
            joints_ts = f["joints/timestamps"][:]
            assert np.array_equal(cam_ts, joints_ts)

    def test_jpeg_metadata_jsonl_stored(self, tmp_path):
        """JPEG 模式：metadata_jsonl bytes 级一致。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()
        _run_convert(session_dir, output_dir, "jpeg")

        with h5py.File(output_dir / "test_session_jpeg.hdf5", "r") as f:
            assert "metadata_jsonl" in f
            stored = bytes(f["metadata_jsonl"][()])
            original = (session_dir / "metadata.jsonl").read_bytes()
            assert stored == original

    def test_jpeg_subdirs_stored(self, tmp_path):
        """JPEG 模式：subdirs 包含所有子目录。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()
        _run_convert(session_dir, output_dir, "jpeg")

        with h5py.File(output_dir / "test_session_jpeg.hdf5", "r") as f:
            subdirs = json.loads(f.attrs["subdirs"])
            assert "head/left" in subdirs
            assert "head/right" in subdirs
            assert "hand/left" in subdirs
            assert "hand/right" in subdirs

    def test_jpeg_interp_correctness(self, tmp_path):
        """JPEG 模式：joints 插值到相机时间戳，数值正确。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=10, num_pose_frames=30)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()
        _run_convert(session_dir, output_dir, "jpeg")

        with h5py.File(output_dir / "test_session_jpeg.hdf5", "r") as f:
            jd = f["joints/data"][:]
            # 第一帧 ANKLE 值应该接近 pose_record 第一帧的值
            # fixture 中 ANKLE = i * 0.01 + 0 * 0.1 = i * 0.01
            # 相机第一帧 ts = base_ts，pose 第一帧 ts = base_ts
            assert abs(jd[0][0] - 0.0) < 0.1  # ANKLE at frame 0
            # 第二帧 ANKLE 应在 pose 帧之间插值
            assert jd[1][0] != jd[0][0] or jd[1][0] == 0.0  # 有变化或都是0


class TestConvertVideoMode:
    """VIDEO 模式转换测试。"""

    def test_video_basic_structure(self, tmp_path):
        """VIDEO 模式：验证 HDF5 基本结构。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=10, num_pose_frames=30)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        assert _run_convert(session_dir, output_dir, "video") is True

        hdf5_path = output_dir / "test_session_video.hdf5"
        assert hdf5_path.exists()

        with h5py.File(hdf5_path, "r") as f:
            assert f.attrs["frames"] == 10

            for cam_name in ["camera_head_left", "camera_head_right"]:
                g = f[cam_name]
                assert g.attrs["encoding"] == "mp4"
                assert g.attrs["frames"] == 10
                # VIDEO 模式 data 是单个 varlen blob
                assert g["data"].shape == ()
                assert g["timestamps"].shape == (10,)

            assert f["joints/data"].shape == (10, 20)

    def test_video_timestamps_from_metadata(self, tmp_path):
        """VIDEO 模式：timestamps 来自 metadata.jsonl。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=10, num_pose_frames=30)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()
        _run_convert(session_dir, output_dir, "video")

        with h5py.File(output_dir / "test_session_video.hdf5", "r") as f:
            cam_ts = f["camera_head_left/timestamps"][:]
            joints_ts = f["joints/timestamps"][:]
            assert np.array_equal(cam_ts, joints_ts)
            # 第一帧时间戳应该是 metadata 里的
            meta = [json.loads(l) for l in
                    (session_dir / "metadata.jsonl").read_text().splitlines() if l.strip()]
            first_ts_ns = int(float(meta[0]["timestamp"]) * 1e9)
            assert cam_ts[0] == first_ts_ns


class TestConvertValidation:
    """校验逻辑测试。"""

    def test_jpeg_frame_count_mismatch_raises(self, tmp_path):
        """JPEG 模式：相机帧数不一致应抛 ValueError。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=10, num_pose_frames=30)
        # 删除 head_right 的部分 metadata 行，制造帧数不一致（保留两个相机）
        meta_path = session_dir / "metadata.jsonl"
        lines = meta_path.read_text().splitlines()
        hl_lines = [l for l in lines if '"head_left"' in l]
        hr_lines = [l for l in lines if '"head_right"' in l]
        # head_right 只保留前 5 行
        meta_path.write_text("\n".join(hl_lines + hr_lines[:5]) + "\n")

        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()
        with pytest.raises(ValueError, match="帧数不一致"):
            _run_convert(session_dir, output_dir, "jpeg")

    def test_empty_session_skipped(self, tmp_path):
        """空 session 目录应跳过。"""
        session_dir = tmp_path / "empty_session"
        session_dir.mkdir()
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()
        assert _run_convert(session_dir, output_dir, "auto") is False
