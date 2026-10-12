from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass, field
from typing import Literal

from PIL import Image, ImageOps

DEFAULT_MAX_BYTES = 4_718_592  # 4.5MB (4.5 * 1024 * 1024) base64 大小上限（对标 Pi / Anthropic）
DEFAULT_MAX_WIDTH = 2000
DEFAULT_MAX_HEIGHT = 2000
DEFAULT_JPEG_QUALITY = 80

SupportedImageMimeType = Literal[
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
]

_MIME_TO_FORMAT: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
    "image/bmp": "BMP",
}

_EXT_TO_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def detect_image_mime_from_extension(filename: str) -> str | None:
    """根据文件后缀识别受支持的图片 MIME 类型。"""
    lower = filename.lower()
    for ext, mime in _EXT_TO_MIME.items():
        if lower.endswith(ext):
            return mime
    return None


@dataclass
class ProcessedImage:
    """图片预处理结果（对标 Pi 的 ImageProcessResult）。"""

    ok: bool
    data: str = ""
    mime_type: str = "image/png"
    hints: list[str] = field(default_factory=list)
    original_width: int = 0
    original_height: int = 0
    width: int = 0
    height: int = 0
    was_resized: bool = False
    error: str = ""


def format_dimension_note(result: ProcessedImage) -> str | None:
    """为经过缩放的图片生成坐标缩放映射说明（对标 Pi 的 formatDimensionNote）。

    这帮助多模态大模型理解缩小后图面坐标与原始物理像素之间的数学对应关系。
    """
    if not result.was_resized or result.width <= 0:
        return None
    scale = result.original_width / result.width
    return (
        f"[Image: original {result.original_width}x{result.original_height}, "
        f"displayed at {result.width}x{result.height}. "
        f"Multiply coordinates by {scale:.2f} to map to original image.]"
    )


def _encode_candidate(img: Image.Image, fmt: str, **kwargs) -> tuple[str, int, str]:
    """尝试将 Pillow 图像编码并返回 (base64_str, encoded_size_bytes, mime_type)。"""
    buf = io.BytesIO()
    # JPEG 不支持 RGBA，遇到透明通道自动转为 RGB
    if fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
        rgb_img = img.convert("RGB")
        rgb_img.save(buf, format="JPEG", **kwargs)
    else:
        img.save(buf, format=fmt, **kwargs)
    raw_bytes = buf.getvalue()
    b64_str = base64.b64encode(raw_bytes).decode("ascii")
    mime = "image/jpeg" if fmt == "JPEG" else ("image/png" if fmt == "PNG" else f"image/{fmt.lower()}")
    return b64_str, len(b64_str.encode("ascii")), mime


def process_image(
    raw_bytes: bytes,
    mime_type: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_width: int = DEFAULT_MAX_WIDTH,
    max_height: int = DEFAULT_MAX_HEIGHT,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> ProcessedImage:
    """对原始图片二进制执行安全预处理（尺寸硬顶、体积硬顶、阶梯降采样及坐标补偿提示）。

    对标 Pi 的 processImage / resizeImageInProcess。
    """
    if not raw_bytes:
        return ProcessedImage(ok=False, error="Empty image payload")

    try:
        raw_img = Image.open(io.BytesIO(raw_bytes))
        # 依据 EXIF 方向信息纠正翻转
        img = ImageOps.exif_transpose(raw_img) or raw_img
    except Exception as exc:
        return ProcessedImage(ok=False, error=f"Image could not be processed: {exc}")

    orig_w, orig_h = img.size
    input_b64_size = math.ceil(len(raw_bytes) / 3) * 4

    # 规范化 MIME
    normalized_mime = mime_type.lower().split(";")[0].strip()
    if normalized_mime == "image/jpg":
        normalized_mime = "image/jpeg"

    # 若未超过任何硬顶限制，原样保留
    if orig_w <= max_width and orig_h <= max_height and input_b64_size < max_bytes:
        b64_data = base64.b64encode(raw_bytes).decode("ascii")
        return ProcessedImage(
            ok=True,
            data=b64_data,
            mime_type=normalized_mime,
            hints=[],
            original_width=orig_w,
            original_height=orig_h,
            width=orig_w,
            height=orig_h,
            was_resized=False,
        )

    # 1. 计算适配 max_width / max_height 的目标尺寸
    target_w = orig_w
    target_h = orig_h
    if target_w > max_width:
        target_h = max(1, round((target_h * max_width) / target_w))
        target_w = max_width
    if target_h > max_height:
        target_w = max(1, round((target_w * max_height) / target_h))
        target_h = max_height

    quality_steps = list(dict.fromkeys([jpeg_quality, 85, 70, 55, 40]))
    curr_w, curr_h = target_w, target_h

    # 2. 循环降采样（渐进式降质量 -> 0.75 阶梯缩减尺寸）
    while True:
        resized_img = img.resize((curr_w, curr_h), Image.Resampling.LANCZOS)
        candidates: list[tuple[str, int, str]] = []

        # 尝试 PNG
        try:
            candidates.append(_encode_candidate(resized_img, "PNG"))
        except Exception:
            pass

        # 尝试阶梯质量 JPEG
        for q in quality_steps:
            try:
                candidates.append(_encode_candidate(resized_img, "JPEG", quality=q))
            except Exception:
                pass

        # 挑选首个符合体积上限的候选
        for b64_data, enc_size, c_mime in candidates:
            if enc_size < max_bytes:
                res = ProcessedImage(
                    ok=True,
                    data=b64_data,
                    mime_type=c_mime,
                    original_width=orig_w,
                    original_height=orig_h,
                    width=curr_w,
                    height=curr_h,
                    was_resized=True,
                )
                note = format_dimension_note(res)
                if note:
                    res.hints.append(note)
                return res

        if curr_w == 1 and curr_h == 1:
            break

        # 阶梯式递减 25% 尺寸
        next_w = 1 if curr_w == 1 else max(1, math.floor(curr_w * 0.75))
        next_h = 1 if curr_h == 1 else max(1, math.floor(curr_h * 0.75))
        if next_w == curr_w and next_h == curr_h:
            break
        curr_w, curr_h = next_w, next_h

    return ProcessedImage(
        ok=False,
        error="[Image omitted: could not be resized below the inline image size limit.]",
    )
