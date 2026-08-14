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

from conftest import _make_mp4_bytes, generate_jpeg_session, generate_video_session


def _run_convert(session_dir: Path, output_dir: Path, fmt: str = "auto", **kwargs):
    """运行转换脚本（直接调用 process_session）。"""
    import convert_to_hdf5
    return convert_to_hdf5.process_session(session_dir, output_dir, fmt, **kwargs)


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

    def test_feedback_is_interpolated_independently(self, tmp_path):
        """存在 feedback_record 时写入独立 feedback 组并对齐参考时间轴。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=4, num_pose_frames=12)
        feedback_frames = [
            {"frame_id": 0, "timestamp": 1000000.0, "data": {"ANKLE": 10.0, "LEFT_GRIPPER": 0.1}},
            {"frame_id": 1, "timestamp": 1000000.1, "data": {"ANKLE": 20.0, "LEFT_GRIPPER": 0.4}},
        ]
        feedback_record = {
            "session_id": session_dir.name,
            "frame_count": len(feedback_frames),
            "frames": feedback_frames,
        }
        (session_dir / f"feedback_record_{session_dir.name}.json").write_text(
            json.dumps(feedback_record), encoding="utf-8")
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        assert _run_convert(session_dir, output_dir, "jpeg") is True

        with h5py.File(output_dir / "test_session_jpeg.hdf5", "r") as f:
            assert "feedback" in f
            feedback = f["feedback"]
            assert json.loads(feedback.attrs["columns"]) == ["ANKLE", "LEFT_GRIPPER"]
            assert feedback["data"].shape == (4, 2)
            assert np.array_equal(feedback["timestamps"][:], f["joints/timestamps"][:])
            assert feedback["data"][0, 0] == pytest.approx(10.0)

    def test_supported_pose_metadata_is_passed_through_to_airs_attrs(self, tmp_path):
        """只透传 auto 模式当前支持的 pose_record metadata。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=4, num_pose_frames=12)
        pose_path = next(session_dir.glob("pose_record_*.json"))
        pose_record = json.loads(pose_path.read_text(encoding="utf-8"))
        pose_record["robot_type"] = "W1_Test"
        pose_record["series_number"] = "SN-REAL-001"
        pose_record["language_prompt"] = "把方块放入盒中"
        pose_record["metadata"] = {
            "system_version": "v1.2.3",
            "hardware_version": "v0.22",
            "ee_type": "hand",
            "ee_name": "DexForce_TestHand",
            "ee_value_range": [0.0, 100.0],
            "intervention_segments": [[1000000.1, 1000000.2]],
        }
        pose_path.write_text(json.dumps(pose_record, ensure_ascii=False), encoding="utf-8")
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        assert _run_convert(session_dir, output_dir, "jpeg") is True

        with h5py.File(output_dir / "test_session_jpeg.hdf5", "r") as f:
            assert f.attrs["robot_type"] == "W1_Test"
            assert f.attrs["series_number"] == "SN-REAL-001"
            assert f.attrs["language"] == "把方块放入盒中"
            assert json.loads(f.attrs["intervention_segments"]) == [
                [1000000.1, 1000000.2]
            ]
            for field in (
                "system_version",
                "hardware_version",
                "ee_type",
                "ee_name",
                "ee_value_range",
            ):
                assert field not in f.attrs
            assert "end_effector_value_range" not in f["joints"].attrs

    def test_unsupported_pose_metadata_is_not_written_or_warned(self, tmp_path, capsys):
        """auto 不支持的设备 metadata 不写入 HDF5，也不产生缺失告警。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=4, num_pose_frames=12)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        assert _run_convert(session_dir, output_dir, "jpeg") is True

        with h5py.File(output_dir / "test_session_jpeg.hdf5", "r") as f:
            assert f.attrs["series_number"] == ""
            assert json.loads(f.attrs["intervention_segments"]) == []
            for field in (
                "system_version",
                "hardware_version",
                "ee_type",
                "ee_name",
                "ee_value_range",
            ):
                assert field not in f.attrs
            assert "end_effector_value_range" not in f["joints"].attrs
        warnings = capsys.readouterr().out
        assert "METADATA_REQUIRED_FIELD_MISSING" not in warnings

    def test_failure_does_not_leave_final_hdf5(self, tmp_path):
        """输入媒体不完整时不得留下可被误认为成功的正式 HDF5。"""
        session_dir = generate_jpeg_session(tmp_path, num_cam_frames=4, num_pose_frames=12)
        (session_dir / "head/right/000002.jpg").unlink()
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        with pytest.raises(ValueError, match="媒体文件不存在"):
            _run_convert(session_dir, output_dir, "jpeg")

        assert not (output_dir / "test_session_jpeg.hdf5").exists()
        assert not (output_dir / "test_session_jpeg.hdf5.partial").exists()


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

    def test_video_frame_mismatch_is_aligned_by_default(self, tmp_path):
        """正常视频帧时间轴不一致时默认生成等帧派生视频。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        meta_path = session_dir / "metadata.jsonl"
        entries = [json.loads(line) for line in meta_path.read_text().splitlines()]
        entries = [
            entry for entry in entries
            if not (entry["camera_type"] == "head_right" and entry["frame_id"] == 2)
        ]
        meta_path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        assert _run_convert(session_dir, output_dir, "video") is True

        with h5py.File(output_dir / "test_session_video.hdf5", "r") as f:
            right = f["camera_head_right"]
            assert right.attrs["frames"] == 5
            assert right["alignment_source_indices"].shape == (5,)
            assert right["alignment_duplicate"].shape == (5,)
            assert int(np.count_nonzero(right["alignment_duplicate"][:])) == 1
            assert right.attrs["alignment_original_frames"] == 4
            assert right.attrs["alignment_aligned_frames"] == 5
            assert right.attrs["alignment_duplicate_frames"] == 1
            assert right.attrs["alignment_dropped_source_frames"] == 0
            assert right.attrs["alignment_max_skew_ms"] == pytest.approx(1000.0 / 30.0)

    def test_video_alignment_exceeding_skew_fails_without_final_file(self, tmp_path):
        """最近源帧偏差超过 33.3ms 时阻止正式 HDF5。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        meta_path = session_dir / "metadata.jsonl"
        entries = [json.loads(line) for line in meta_path.read_text().splitlines()]
        for entry in entries:
            if entry["camera_type"] == "head_right":
                entry["timestamp"] += 0.2
                entry["ros_timestamp"] += 0.2
        meta_path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        with pytest.raises(ValueError, match="ALIGNMENT_SKEW_EXCEEDED"):
            _run_convert(session_dir, output_dir, "video")

        assert not (output_dir / "test_session_video.hdf5").exists()

    def test_abnormal_bitrate_requires_explicit_repair(self, tmp_path):
        """异常平均码率默认拒绝，显式修复后保留原片并完成转换。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        original_video = session_dir / "head/left/video.mp4"
        original_size = original_video.stat().st_size
        with original_video.open("ab") as stream:
            stream.write(b"\0" * 500_000)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        with pytest.raises(ValueError, match="VIDEO_BITRATE_EXCEEDED"):
            _run_convert(session_dir, output_dir, "video")

        repair_dir = tmp_path / "repaired"
        assert _run_convert(
            session_dir,
            output_dir,
            "video",
            repair_video=True,
            repair_dir=repair_dir,
        ) is True
        repaired_video = repair_dir / session_dir.name / "head/left/video.mp4"
        assert repaired_video.is_file()
        assert repaired_video.stat().st_size < original_video.stat().st_size
        assert original_video.stat().st_size > original_size

    def test_noncompliant_h264_requires_explicit_repair(self, tmp_path):
        """非 Baseline 或带 B 帧的 H.264 默认拒绝，显式修复后才交付。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        source_video = session_dir / "head/right/video.mp4"
        source_video.write_bytes(_make_mp4_bytes(5, compliant=False))
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        with pytest.raises(ValueError, match="VIDEO_ENCODING_NONCOMPLIANT"):
            _run_convert(session_dir, output_dir, "video")

        repair_dir = tmp_path / "repaired"
        assert _run_convert(
            session_dir,
            output_dir,
            "video",
            repair_video=True,
            repair_dir=repair_dir,
        ) is True
        repaired_video = repair_dir / session_dir.name / "head/right/video.mp4"
        assert repaired_video.is_file()

    def test_truncated_video_fails_full_decode_before_hdf5(self, tmp_path):
        """容器头存在但媒体截断时，完整解码校验必须阻止正式 HDF5。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=30, num_pose_frames=90)
        source_video = session_dir / "head/right/video.mp4"
        source_bytes = source_video.read_bytes()
        source_video.write_bytes(source_bytes[: len(source_bytes) // 2])
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        with pytest.raises(ValueError, match="视频解析失败|VIDEO_FRAME_COUNT_MISMATCH"):
            _run_convert(session_dir, output_dir, "video")

        assert not (output_dir / "test_session_video.hdf5").exists()

    def test_video_size_hard_limit_fails_before_hdf5_creation(self, tmp_path):
        """单路视频超过配置硬限制时输出可定位错误且不创建正式文件。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        with pytest.raises(
            ValueError,
            match="VIDEO_SIZE_LIMIT_EXCEEDED.*suggestion=.*--repair-video",
        ):
            _run_convert(
                session_dir,
                output_dir,
                "video",
                max_video_bytes=100,
            )

        assert not (output_dir / "test_session_video.hdf5").exists()

    def test_dynamic_memory_gate_fails_before_hdf5_creation(self, tmp_path, monkeypatch):
        """预计峰值加保留内存超过 MemAvailable 时应在 blob 写入前拒绝。"""
        import convert_to_hdf5

        session_dir = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()
        monkeypatch.setattr(convert_to_hdf5, "get_mem_available_bytes", lambda: 1_000)

        with pytest.raises(
            ValueError,
            match="VIDEO_MEMORY_LIMIT_EXCEEDED.*suggestion=释放内存",
        ):
            _run_convert(
                session_dir,
                output_dir,
                "video",
                memory_peak_factor=1.0,
                memory_reserve_bytes=100,
            )

        assert not (output_dir / "test_session_video.hdf5").exists()

    def test_video_embedding_does_not_use_path_read_bytes(self, tmp_path, monkeypatch):
        """VIDEO 标量 blob 使用逐路 memmap，不通过 Path.read_bytes 建立 Python 大字节串。"""
        session_dir = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        def reject_read_bytes(_path):
            raise AssertionError("VIDEO 转换不得调用 Path.read_bytes")

        monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)
        assert _run_convert(session_dir, output_dir, "video") is True

    def test_post_write_validation_failure_removes_partial(self, tmp_path, monkeypatch):
        """HDF5 写完后的校验失败也只能留下错误，不得留下 partial 或正式文件。"""
        import convert_to_hdf5

        session_dir = generate_video_session(tmp_path, num_cam_frames=5, num_pose_frames=15)
        output_dir = tmp_path / "hdf5_output"
        output_dir.mkdir()

        def reject_structure(*_args, **_kwargs):
            raise ValueError("forced validation failure")

        monkeypatch.setattr(convert_to_hdf5, "validate_hdf5_structure", reject_structure)
        with pytest.raises(ValueError, match="forced validation failure"):
            _run_convert(session_dir, output_dir, "video")

        assert not (output_dir / "test_session_video.hdf5").exists()
        assert not (output_dir / "test_session_video.hdf5.partial").exists()


class TestConvertValidation:
    """校验逻辑测试。"""

    @staticmethod
    def _write_legacy_video_hdf5(path: Path, assign_video: bool) -> None:
        """写入不带新对齐追溯数据的旧版最小 HDF5。"""
        import convert_to_hdf5

        with h5py.File(path, "w") as h5:
            h5.attrs["frames"] = 2
            h5.attrs["cameras"] = json.dumps(["head_left"])
            joints = h5.create_group("joints")
            joints.create_dataset("data", data=np.zeros((2, 1), dtype=np.float32))
            camera = h5.create_group("camera_head_left")
            camera.attrs["encoding"] = "mp4"
            camera.create_dataset("timestamps", data=np.arange(2, dtype=np.uint64))
            data = camera.create_dataset(
                "data", shape=(), dtype=h5py.vlen_dtype(np.dtype("uint8")))
            if assign_video:
                data[()] = np.arange(16, dtype=np.uint8)

        assert convert_to_hdf5.is_valid_existing_hdf5(path) is assign_video

    def test_valid_legacy_video_hdf5_can_still_be_skipped(self, tmp_path):
        """旧版已赋值标量 MP4 不因缺少新追溯 dataset 被误判损坏。"""
        self._write_legacy_video_hdf5(tmp_path / "legacy_valid.hdf5", assign_video=True)

    def test_unassigned_legacy_video_blob_is_not_skipped(self, tmp_path):
        """被 kill 前未完成赋值的标量 MP4 不得视为完整文件。"""
        self._write_legacy_video_hdf5(tmp_path / "legacy_incomplete.hdf5", assign_video=False)

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
