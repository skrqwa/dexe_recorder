# ----------------------------------------------------------------------------
# Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""视频对齐派生文件的原子发布测试。"""

import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import video_pipeline


def test_alignment_can_reference_advertised_tail_frame(tmp_path):
    """旧 PyAV 少解码一个尾帧时，最近邻对齐仍应生成完整派生视频。"""
    from conftest import _make_mp4_bytes

    source_path = tmp_path / "source.mp4"
    output_path = tmp_path / "derived.mp4"
    source_path.write_bytes(_make_mp4_bytes(5))

    info = video_pipeline.align_video(source_path, output_path, list(range(5)))

    assert output_path.is_file()
    assert info.frame_count == 5


def test_invalid_derived_video_is_not_published(tmp_path, monkeypatch):
    """派生视频校验失败时不得留下正式文件或 partial。"""
    source_path = tmp_path / "source.mp4"
    output_path = tmp_path / "derived.mp4"
    source_path.write_bytes(b"source")

    def fake_probe(path):
        frame_count = 2 if Path(path) == source_path else 1
        return video_pipeline.VideoInfo(width=192, height=144, frame_count=frame_count, codec="h264")

    def fake_encode(_frames, partial_path, _width, _height, _fps):
        Path(partial_path).write_bytes(b"invalid-derived-video")
        return 2

    monkeypatch.setattr(video_pipeline, "probe_video", fake_probe)
    monkeypatch.setattr(video_pipeline, "_encode_with_gstreamer", fake_encode)

    with pytest.raises(ValueError, match="派生视频校验失败"):
        video_pipeline.align_video(source_path, output_path, [0, 1])

    assert not output_path.exists()
    assert not output_path.with_suffix(".mp4.partial").exists()
