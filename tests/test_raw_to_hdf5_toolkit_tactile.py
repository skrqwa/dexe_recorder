"""Behavior tests for raw teleoperation tactile HDF5 conversion."""

import importlib.util
import json
from pathlib import Path

import cv2
import h5py
import numpy as np
import pytest


MODULE_PATH = Path(__file__).parent / "raw_to_hdf5_toolkit" / "raw_to_hdf5.py"
SPEC = importlib.util.spec_from_file_location("raw_to_hdf5_toolkit_tactile", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _create_session(tmp_path: Path) -> Path:
    """Create a minimal three-frame image and pose session."""
    session_dir = tmp_path / "session"
    image_dir = session_dir / "head" / "left"
    image_dir.mkdir(parents=True)
    timestamps = (1787105702.0, 1787105702.1, 1787105702.2)
    metadata_lines = []
    pose_frames = []
    for index, timestamp in enumerate(timestamps):
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
        pose_frames.append({"timestamp": timestamp, "data": {"ANKLE": float(index)}})
    (session_dir / "metadata.jsonl").write_text(
        "\n".join(metadata_lines) + "\n", encoding="utf-8")
    (session_dir / "pose_record_session.json").write_text(
        json.dumps({"frames": pose_frames}), encoding="utf-8")
    return session_dir


def _state(field_name: str, force, torque=(0.0, 0.0, 0.0)) -> dict:
    """Create one verified 1x1 tactile field."""
    return {
        "field_name": field_name,
        "distributed_datas": [
            {
                "force": dict(zip(("x", "y", "z"), force)),
                "torque": dict(zip(("x", "y", "z"), torque)),
            }
        ],
        "row": 1,
        "col": 1,
    }


def test_legacy_jsonl_keeps_raw_samples_and_skips_empty_hand(tmp_path):
    """Legacy tactile rows remain at source rate and empty hand placeholders are omitted."""
    session_dir = _create_session(tmp_path)
    rows = [
        {
            "ts": 1787105702.01,
            "hand": "right",
            "tactile_states": [
                _state("FINGER1", (1.0, 2.0, 3.0)),
                _state("FINGER2", (4.0, 5.0, 6.0)),
            ],
        },
        {
            "ts": 1787105702.02,
            "hand": "left",
            "tactile_states": [
                {"field_name": "", "distributed_datas": [], "row": 0, "col": 0}
            ],
        },
        {
            "ts": 1787105702.21,
            "hand": "right",
            "tactile_states": [
                _state("FINGER1", (7.0, 8.0, 9.0)),
                _state("FINGER2", (10.0, 11.0, 12.0)),
            ],
        },
        {
            "ts": 1787105702.22,
            "hand": "left",
            "tactile_states": [
                {"field_name": "", "distributed_datas": [], "row": 0, "col": 0}
            ],
        },
    ]
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
    (session_dir / "tactile.jsonl").write_bytes(raw)
    output_path = tmp_path / "session.hdf5"

    MODULE.create_standard_hdf5(
        session_dir, output_path, MODULE.load_config(), fmt="jpeg")

    with h5py.File(output_path, "r") as h5_file:
        assert "tactile" in h5_file
        assert "left" not in h5_file["tactile"]
        right = h5_file["tactile/right"]
        assert right.attrs["frames"] == 2
        assert right.attrs["sample_rate"] == pytest.approx(5.0)
        assert json.loads(right["force"].attrs["columns"]) == [
            "FINGER1_x",
            "FINGER1_y",
            "FINGER1_z",
            "FINGER2_x",
            "FINGER2_y",
            "FINGER2_z",
        ]
        assert np.array_equal(
            right["force/data"][:],
            np.asarray(
                [[1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11, 12]],
                dtype=np.float32,
            ),
        )
        assert right["force/data"].shape[0] != h5_file.attrs["frames"]
        assert np.array_equal(
            right["force/timestamps"][:],
            np.asarray([1787105702010000000, 1787105702210000000], dtype=np.uint64),
        )
        assert np.array_equal(right["torque/timestamps"][:], right["force/timestamps"][:])
        assert bytes(h5_file["tactile_jsonl"][:]) == raw


def test_structured_json_uses_per_hand_source_timestamps_without_resampling(tmp_path):
    """Structured tactile frames keep each hand's source time and independent schema."""
    session_dir = _create_session(tmp_path)
    payload = {
        "session_id": "session",
        "frames": [
            {
                "frame_id": 0,
                "timestamp": 1787105809000,
                "data": {
                    "left": {
                        "timestamp": 1787105702000,
                        "tactile_states": [_state("LEFT_PAD", (1.0, 2.0, 3.0))],
                    }
                },
            },
            {
                "frame_id": 1,
                "timestamp": 1787105809100,
                "data": {
                    "right": {
                        "timestamp": 1787105702050,
                        "tactile_states": [_state("RIGHT_PAD", (4.0, 5.0, 6.0))],
                    }
                },
            },
            {
                "frame_id": 2,
                "timestamp": 1787105809200,
                "data": {
                    "left": {
                        "timestamp": 1787105702100,
                        "tactile_states": [_state("LEFT_PAD", (7.0, 8.0, 9.0))],
                    }
                },
            },
        ],
    }
    raw = json.dumps(payload).encode()
    (session_dir / "tactile.json").write_bytes(raw)
    output_path = tmp_path / "session.hdf5"

    MODULE.create_standard_hdf5(
        session_dir, output_path, MODULE.load_config(), fmt="jpeg")

    with h5py.File(output_path, "r") as h5_file:
        left = h5_file["tactile/left"]
        right = h5_file["tactile/right"]
        assert left.attrs["frames"] == 2
        assert right.attrs["frames"] == 1
        assert left.attrs["sample_rate"] == pytest.approx(10.0)
        assert right.attrs["sample_rate"] == pytest.approx(0.0)
        assert json.loads(left["force"].attrs["columns"]) == [
            "LEFT_PAD_x",
            "LEFT_PAD_y",
            "LEFT_PAD_z",
        ]
        assert json.loads(right["force"].attrs["columns"]) == [
            "RIGHT_PAD_x",
            "RIGHT_PAD_y",
            "RIGHT_PAD_z",
        ]
        assert np.array_equal(
            left["force/timestamps"][:],
            np.asarray([1787105702000000000, 1787105702100000000], dtype=np.uint64),
        )
        assert np.array_equal(
            right["force/timestamps"][:],
            np.asarray([1787105702050000000], dtype=np.uint64),
        )
        assert bytes(h5_file["tactile_json"][:]) == raw


def test_empty_tactile_file_is_preserved_without_fabricated_group(tmp_path):
    """An empty legacy file remains reversible but does not create tactile samples."""
    session_dir = _create_session(tmp_path)
    (session_dir / "tactile.jsonl").write_bytes(b"")
    output_path = tmp_path / "session.hdf5"

    MODULE.create_standard_hdf5(
        session_dir, output_path, MODULE.load_config(), fmt="jpeg")

    with h5py.File(output_path, "r") as h5_file:
        assert "tactile" not in h5_file
        assert h5_file["tactile_jsonl"].shape == (0,)


def test_multi_taxel_array_is_rejected_instead_of_silently_truncated(tmp_path):
    """Unconfirmed row/column ordering must fail instead of reading only point zero."""
    session_dir = _create_session(tmp_path)
    state = _state("PAD", (1.0, 2.0, 3.0))
    state["row"] = 1
    state["col"] = 2
    state["distributed_datas"].append(state["distributed_datas"][0])
    (session_dir / "tactile.jsonl").write_text(
        json.dumps(
            {
                "ts": 1787105702.01,
                "hand": "right",
                "tactile_states": [state],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"TACTILE_ARRAY_UNSUPPORTED.*hand=right.*row=1 col=2 points=2",
    ):
        MODULE.create_standard_hdf5(
            session_dir, tmp_path / "session.hdf5", MODULE.load_config(), fmt="jpeg")


def test_new_and_legacy_tactile_files_are_rejected_as_ambiguous(tmp_path):
    """A session cannot silently choose one of two tactile sources."""
    session_dir = _create_session(tmp_path)
    (session_dir / "tactile.jsonl").write_bytes(b"")
    (session_dir / "tactile.json").write_text(
        json.dumps({"frames": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="TACTILE_MULTIPLE_INPUT_FILES"):
        MODULE.create_standard_hdf5(
            session_dir, tmp_path / "session.hdf5", MODULE.load_config(), fmt="jpeg")
