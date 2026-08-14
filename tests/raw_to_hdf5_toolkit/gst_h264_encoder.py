# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""gst_h264_encoder.py — GStreamer nvv4l2h264enc 硬件 MP4 编码器。

管线: appsrc(RGB) → videoconvert → nvvidconv → nvv4l2h264enc → h264parse → qtmux → filesink
逐帧推流，不积压内存。编码完成后读回 MP4 bytes。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
from PIL import Image

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)


def _build_pipeline(output_path: str, width: int, height: int, fps: int) -> tuple:
    pipeline = Gst.Pipeline.new("h264-mp4")

    appsrc = Gst.ElementFactory.make("appsrc", "source")
    appsrc.set_property("format", Gst.Format.TIME)
    appsrc.set_property("is-live", False)
    caps = Gst.Caps.from_string(f"video/x-raw,format=RGB,width={width},height={height},framerate={fps}/1")
    appsrc.set_property("caps", caps)
    pipeline.add(appsrc)

    videoconvert = Gst.ElementFactory.make("videoconvert", "convert")
    pipeline.add(videoconvert)

    nvvidconv = Gst.ElementFactory.make("nvvidconv", "nvconv")
    pipeline.add(nvvidconv)
    nvmm_caps = Gst.Caps.from_string("video/x-raw(memory:NVMM),format=I420")
    capsfilter = Gst.ElementFactory.make("capsfilter", "nvmm-filter")
    capsfilter.set_property("caps", nvmm_caps)
    pipeline.add(capsfilter)

    encoder = Gst.ElementFactory.make("nvv4l2h264enc", "encoder")
    encoder.set_property("bitrate", 4000000)
    encoder.set_property("control-rate", 0)  # VBR
    encoder.set_property("peak-bitrate", 8000000)
    encoder.set_property("profile", 0)  # Baseline（无 B 帧）
    encoder.set_property("num-B-Frames", 0)  # 显式禁用 B 帧
    encoder.set_property("idrinterval", 30)  # 固定 GOP=30
    encoder.set_property("iframeinterval", 30)  # I 帧间隔=30
    encoder.set_property("insert-sps-pps", True)  # 每个 IDR 插入 SPS/PPS
    pipeline.add(encoder)

    parser = Gst.ElementFactory.make("h264parse", "parser")
    pipeline.add(parser)

    muxer = Gst.ElementFactory.make("qtmux", "muxer")
    pipeline.add(muxer)

    sink = Gst.ElementFactory.make("filesink", "sink")
    sink.set_property("location", output_path)
    pipeline.add(sink)

    appsrc.link(videoconvert)
    videoconvert.link(nvvidconv)
    nvvidconv.link(capsfilter)
    capsfilter.link(encoder)
    encoder.link(parser)
    parser.link(muxer)
    muxer.link(sink)

    return pipeline, appsrc


def _push_frames(appsrc, frames: Iterator[Image.Image], fps: int) -> int:
    count = 0
    frame_duration_ns = Gst.SECOND // fps
    for i, img in enumerate(frames):
        arr = np.array(img.convert("RGB"), dtype=np.uint8)
        buf = Gst.Buffer.new_wrapped(arr.tobytes())
        buf.pts = buf.dts = i * frame_duration_ns
        buf.duration = frame_duration_ns
        appsrc.emit("push-buffer", buf)
        count += 1
    appsrc.emit("end-of-stream")
    return count


def _wait_eos(pipeline, timeout_s: float = 300.0) -> bool:
    bus = pipeline.get_bus()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        msg = bus.timed_pop_filtered(int(1e9), Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if msg is None:
            continue
        if msg.type == Gst.MessageType.EOS:
            return True
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[gst_h264] ERROR: {err} | {dbg}")
            return False
    return False


def encode_frames_to_mp4(
    frames: Iterator[Image.Image],
    width: int,
    height: int,
    fps: int = 30,
    timeout_s: float = 300.0,
) -> Optional[bytes]:
    """将 PIL Image 迭代器编码为 MP4 bytes（GPU 硬编，逐帧推流，不积压）。

    Args:
        frames: PIL Image 迭代器（只遍历一次）
        width: 帧宽
        height: 帧高
        fps: 帧率
        timeout_s: 超时秒数

    Returns:
        MP4 字节串，失败返回 None
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        pipeline, appsrc = _build_pipeline(tmp_path, width, height, fps)
        pipeline.set_state(Gst.State.PLAYING)

        count = _push_frames(appsrc, frames, fps)
        if count == 0:
            pipeline.set_state(Gst.State.NULL)
            return None

        ok = _wait_eos(pipeline, timeout_s)
        pipeline.set_state(Gst.State.NULL)

        if not ok:
            return None

        mp4_bytes = Path(tmp_path).read_bytes()
        return mp4_bytes if len(mp4_bytes) > 0 else None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
