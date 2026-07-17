# ----------------------------------------------------------------------------
# Copyright (c) 2021-2025 DexForce Technology Co., Ltd.
#
# All rights reserved.
# ----------------------------------------------------------------------------
"""image_video_encode.py - 视频解码库（从遥操移植，仅保留解码部分）。

依赖：PyAV（pip install av）、Pillow
"""

import io
import logging
from typing import List

import av
from PIL import Image

logger = logging.getLogger(__name__)


def decode_mp4_bytes_to_rgb_images(encoded_bytes: bytes) -> List[Image.Image]:
    """将 MP4 字节流解码为 RGB PIL Image 列表。

    Args:
        encoded_bytes: MP4 视频的字节流。

    Returns:
        PIL Image 列表（RGB 模式）。
    """
    input_buffer = io.BytesIO(encoded_bytes)
    container = av.open(input_buffer, "r")

    images: List[Image.Image] = []
    for frame in container.decode(video=0):
        img = frame.to_image()
        images.append(img)

    container.close()
    return images


__all__ = ["decode_mp4_bytes_to_rgb_images"]
