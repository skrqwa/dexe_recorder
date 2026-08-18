"""Tests for nested teleoperation feedback conversion."""

import importlib.util
import json
from pathlib import Path

import cv2
import h5py
import numpy as np
import pytest


MODULE_PATH = Path(__file__).parent / "raw_to_hdf5_toolkit" / "raw_to_hdf5.py"
SPEC = importlib.util.spec_from_file_location("raw_to_hdf5_toolkit", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _feedback_frame(frame_id, timestamp_ms, robot_value, left_value, right_value):
    """Build one nested feedback frame with upstream-normalized names."""
    return {
        "frame_id": frame_id,
        "timestamp": timestamp_ms,
        "data": {
            "robot_state": {
                "timestamp": timestamp_ms - 1,
                "joints": {"ANKLE": robot_value},
            },
            "ee": {
                "left": {
                    "timestamp": timestamp_ms - 30,
                    "joints": {"LEFT_T_MCP": left_value},
                },
                "right": {
                    "timestamp": timestamp_ms - 40,
                    "joints": {"RIGHT_T_MCP": right_value},
                },
            },
        },
    }


def test_feedback_uses_outer_timestamp_and_preserves_upstream_names(tmp_path):
    """The loader flattens joints without reading inner timestamps or renaming keys."""
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text(
        json.dumps(
            {
                "frames": [
                    _feedback_frame(0, 1_786_947_260_000, 1.0, 2.0, 3.0),
                    _feedback_frame(1, 1_786_947_260_100, 4.0, 5.0, 6.0),
                ]
            }
        ),
        encoding="utf-8",
    )

    timestamps, values, columns = MODULE.load_feedback_and_joint_keys(feedback_path)

    assert timestamps.tolist() == pytest.approx([1786947260.0, 1786947260.1])
    assert columns == ["ANKLE", "LEFT_T_MCP", "RIGHT_T_MCP"]
    assert np.allclose(
        values,
        np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32),
    )


def test_feedback_rejects_duplicate_joint_names(tmp_path):
    """Duplicate upstream names fail instead of silently overwriting feedback."""
    frame = _feedback_frame(0, 1_000_000, 1.0, 2.0, 3.0)
    frame["data"]["ee"]["left"]["joints"] = {"ANKLE": 2.0}
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text(json.dumps({"frames": [frame]}), encoding="utf-8")

    with pytest.raises(ValueError, match="FEEDBACK_JOINT_NAME_CONFLICT.*ANKLE"):
        MODULE.load_feedback_and_joint_keys(feedback_path)


def test_hdf5_contains_aligned_feedback_group(tmp_path):
    """Nested feedback is aligned to the camera axis and written independently."""
    session_dir = tmp_path / "session"
    image_dir = session_dir / "head" / "left"
    image_dir.mkdir(parents=True)
    metadata_lines = []
    for index, timestamp in enumerate((1786947260.0, 1786947260.1)):
        relative_path = f"head/left/{index:06d}.jpg"
        assert cv2.imwrite(
            str(session_dir / relative_path),
            np.full((2, 3, 3), index, dtype=np.uint8),
        )
        metadata_lines.append(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "camera_type": "head_left",
                    "image_path": relative_path,
                }
            )
        )
    (session_dir / "metadata.jsonl").write_text(
        "\n".join(metadata_lines) + "\n", encoding="utf-8")
    (session_dir / "pose_record_session.json").write_text(
        json.dumps(
            {
                "frames": [
                    {"timestamp": 1786947260.0, "data": {"ANKLE": 10.0}},
                    {"timestamp": 1786947260.1, "data": {"ANKLE": 20.0}},
                ]
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "feedback.json").write_text(
        json.dumps(
            {
                "frames": [
                    _feedback_frame(0, 1_786_947_260_000, 1.0, 2.0, 3.0),
                    _feedback_frame(1, 1_786_947_260_100, 4.0, 5.0, 6.0),
                ]
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "session.hdf5"

    MODULE.create_standard_hdf5(
        session_dir, output_path, MODULE.load_config(), fmt="jpeg")

    with h5py.File(output_path, "r") as h5_file:
        assert json.loads(h5_file["feedback"].attrs["columns"]) == [
            "ANKLE",
            "LEFT_T_MCP",
            "RIGHT_T_MCP",
        ]
        assert h5_file["feedback/data"].shape == (2, 3)
        assert np.array_equal(
            h5_file["feedback/timestamps"][:], h5_file["joints/timestamps"][:])
        assert np.allclose(
            h5_file["feedback/data"][:],
            np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32),
        )
