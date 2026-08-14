#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# Copyright (c) 2021-2026 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""MP4 探测、按帧映射对齐以及 H.264 派生编码。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import av
import numpy as np

MIN_JETSON_ENCODER_DIMENSION = 144


@dataclass(frozen=True)
class VideoInfo:
    """转换器需要的稳定视频属性。"""

    width: int
    height: int
    frame_count: int
    codec: str
    duration_sec: float = 0.0
    average_rate: float = 0.0
    profile: str = ""
    pixel_format: str = ""
    has_b_frames: bool = False
    max_gop: int = 0


def probe_video(path: Path) -> VideoInfo:
    """完整解码 MP4 并返回稳定的容器、编码与 GOP 属性。"""
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"视频不存在或为空: {path}")
    try:
        with av.open(str(path), mode="r") as container:
            if not container.streams.video:
                raise ValueError(f"视频流不存在: {path}")
            stream = container.streams.video[0]
            advertised_frames = int(stream.frames or 0)
            frame_count = 0
            keyframe_indices = []
            last_end_sec = 0.0
            average_rate = float(stream.average_rate) if stream.average_rate else 0.0
            for frame_count, frame in enumerate(container.decode(stream), start=1):
                if frame.key_frame:
                    keyframe_indices.append(frame_count - 1)
                frame_start_sec = float(frame.time or 0.0)
                frame_duration = getattr(frame, "duration", None)
                frame_duration_sec = (
                    float(frame_duration * stream.time_base)
                    if frame_duration is not None
                    else (1.0 / average_rate if average_rate > 0.0 else 0.0)
                )
                last_end_sec = max(last_end_sec, frame_start_sec + frame_duration_sec)
            if frame_count <= 0:
                raise ValueError(f"视频无可解码帧: {path}")
            if advertised_frames > 0 and advertised_frames != frame_count:
                # PyAV 8.1 on Jetson can omit the delayed tail frame of short
                # H.264 streams even though the MP4 sample table is complete.
                # Accept only that known one-frame discrepancy; larger gaps
                # still identify truncated or otherwise incomplete media.
                if advertised_frames - frame_count == 1:
                    frame_count = advertised_frames
                else:
                    raise ValueError(
                        "VIDEO_FRAME_COUNT_MISMATCH "
                        f"path={path} advertised={advertised_frames} decoded={frame_count}")
            max_gop = 0
            if keyframe_indices:
                keyframe_boundaries = keyframe_indices + [frame_count]
                max_gop = max(
                    current - previous
                    for previous, current in zip(keyframe_boundaries, keyframe_boundaries[1:]))
            codec_context = stream.codec_context
            return VideoInfo(
                width=int(codec_context.width),
                height=int(codec_context.height),
                frame_count=frame_count,
                codec=str(codec_context.name),
                duration_sec=last_end_sec,
                average_rate=average_rate,
                profile=str(codec_context.profile or ""),
                pixel_format=str(codec_context.format.name if codec_context.format else ""),
                has_b_frames=bool(codec_context.has_b_frames),
                max_gop=max_gop,
            )
    except (av.error.FFmpegError, OSError) as error:
        raise ValueError(f"视频解析失败: path={path}, error={error}") from error


def _iter_aligned_rgb_frames(
    source_path: Path,
    source_indices: Sequence[int],
    advertised_frame_count: int,
) -> Iterator[np.ndarray]:
    """按非递减源帧索引流式产生 RGB 帧，并兼容 PyAV 8.1 尾帧延迟。"""
    if any(current < previous for previous, current in zip(source_indices, source_indices[1:])):
        raise ValueError("对齐源帧索引必须非递减")

    target_index = 0
    decoded_count = 0
    last_array = None
    with av.open(str(source_path), mode="r") as container:
        for source_index, frame in enumerate(container.decode(video=0)):
            decoded_count = source_index + 1
            last_array = frame.to_ndarray(format="rgb24")
            while target_index < len(source_indices) and source_indices[target_index] == source_index:
                yield last_array
                target_index += 1
            if target_index >= len(source_indices):
                break
    if (
        target_index < len(source_indices)
        and last_array is not None
        and advertised_frame_count == decoded_count + 1
    ):
        # PyAV 8.1 on Jetson does not expose the delayed final frame of some
        # short H.264 streams. The MP4 sample table still proves exactly one
        # terminal sample exists, so nearest alignment reuses the last
        # decodable frame rather than making the whole session unconvertible.
        while (
            target_index < len(source_indices)
            and source_indices[target_index] == decoded_count
        ):
            yield last_array
            target_index += 1
    if target_index != len(source_indices):
        raise ValueError(
            f"视频帧不足: path={source_path}, requested_index={source_indices[target_index]}, "
            f"written={target_index}/{len(source_indices)}")


def _encode_with_pyav(
    frames: Iterable[np.ndarray], output_path: Path, width: int, height: int, fps: int
) -> int:
    """使用 libx264 编码，作为无 Jetson GStreamer 环境时的测试后备。"""
    count = 0
    with av.open(str(output_path), mode="w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        stream.bit_rate = 4_000_000
        stream.options = {"profile": "baseline", "g": str(fps), "bf": "0"}
        for array in frames:
            frame = av.VideoFrame.from_ndarray(array, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
            count += 1
        for packet in stream.encode():
            container.mux(packet)
    return count


def _encode_with_gstreamer(
    frames: Iterable[np.ndarray], output_path: Path, width: int, height: int, fps: int
) -> int:
    """使用 Jetson nvv4l2h264enc 流式编码为目标 AIRS H.264 MP4。"""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    pipeline_text = (
        f'appsrc name=source format=time is-live=false block=true max-bytes=8388608 '
        f'caps="video/x-raw,format=RGB,width={width},height={height},framerate={fps}/1" '
        "! videoconvert ! nvvidconv compute-hw=1 "
        "! video/x-raw(memory:NVMM),format=I420 "
        "! nvv4l2h264enc bitrate=4000000 control-rate=0 peak-bitrate=8000000 "
        "idrinterval=30 iframeinterval=30 insert-sps-pps=true profile=0 num-B-Frames=0 "
        f'! h264parse ! qtmux ! filesink location="{output_path}"'
    )
    pipeline = Gst.parse_launch(pipeline_text)
    appsrc = pipeline.get_by_name("source")
    pipeline.set_state(Gst.State.PLAYING)
    duration = Gst.SECOND // fps
    count = 0
    try:
        for count, array in enumerate(frames, start=1):
            buffer = Gst.Buffer.new_wrapped(array.tobytes())
            buffer.pts = buffer.dts = (count - 1) * duration
            buffer.duration = duration
            result = appsrc.emit("push-buffer", buffer)
            if result != Gst.FlowReturn.OK:
                raise ValueError(f"GStreamer push-buffer 失败: flow={result}")
        appsrc.emit("end-of-stream")
        bus = pipeline.get_bus()
        message = bus.timed_pop_filtered(
            300 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if message is None:
            raise ValueError("GStreamer 编码等待 EOS 超时")
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            raise ValueError(f"GStreamer 编码失败: {error}; {debug}")
        return count
    finally:
        pipeline.set_state(Gst.State.NULL)


def align_video(
    source_path: Path,
    output_path: Path,
    source_indices: Sequence[int],
    fps: int = 30,
) -> VideoInfo:
    """按索引映射生成等帧派生视频，Jetson 优先使用硬件编码。"""
    source_info = probe_video(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_suffix(output_path.suffix + ".partial")
    partial_path.unlink(missing_ok=True)
    frames = _iter_aligned_rgb_frames(
        source_path, source_indices, source_info.frame_count)
    try:
        if (
            source_info.width < MIN_JETSON_ENCODER_DIMENSION
            or source_info.height < MIN_JETSON_ENCODER_DIMENSION
        ):
            count = _encode_with_pyav(
                frames, partial_path, source_info.width, source_info.height, fps)
        else:
            try:
                count = _encode_with_gstreamer(
                    frames, partial_path, source_info.width, source_info.height, fps)
            except (ImportError, ValueError):
                frames = _iter_aligned_rgb_frames(
                    source_path, source_indices, source_info.frame_count)
                count = _encode_with_pyav(
                    frames, partial_path, source_info.width, source_info.height, fps)
        if count != len(source_indices):
            raise ValueError(f"派生视频帧数错误: expected={len(source_indices)}, actual={count}")
        output_info = probe_video(partial_path)
        if output_info.frame_count != len(source_indices):
            raise ValueError(
                f"派生视频校验失败: expected={len(source_indices)}, actual={output_info.frame_count}")
        expected_duration = len(source_indices) / fps
        if abs(output_info.duration_sec - expected_duration) > (1.0 / fps + 1e-6):
            raise ValueError(
                "派生视频时长校验失败: "
                f"expected={expected_duration:.6f}, actual={output_info.duration_sec:.6f}")
        os.replace(partial_path, output_path)
        return output_info
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise
