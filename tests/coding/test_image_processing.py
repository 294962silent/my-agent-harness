from __future__ import annotations

import base64
import io

from PIL import Image

from my_coding_agent.image_processing import (
    ProcessedImage,
    format_dimension_note,
    process_image,
)


def _create_test_image_bytes(width: int, height: int, fmt: str = "PNG", color: str = "blue") -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_process_image_small_png_untouched() -> None:
    raw_bytes = _create_test_image_bytes(100, 100, "PNG")
    res = process_image(raw_bytes, "image/png")

    assert res.ok is True
    assert res.was_resized is False
    assert res.width == 100
    assert res.height == 100
    assert res.original_width == 100
    assert res.original_height == 100
    assert res.mime_type == "image/png"
    assert len(res.hints) == 0

    decoded = base64.b64decode(res.data)
    assert len(decoded) == len(raw_bytes)


def test_process_image_large_dimension_downsampled() -> None:
    raw_bytes = _create_test_image_bytes(3840, 2160, "PNG")
    res = process_image(raw_bytes, "image/png")

    assert res.ok is True
    assert res.was_resized is True
    assert res.original_width == 3840
    assert res.original_height == 2160
    assert res.width == 2000
    assert res.height == 1125

    # 检查坐标补偿说明
    note = next((h for h in res.hints if "Multiply coordinates by" in h), None)
    assert note is not None
    assert "original 3840x2160" in note
    assert "displayed at 2000x1125" in note
    assert "Multiply coordinates by 1.92 to map to original image." in note


def test_process_image_format_dimension_note() -> None:
    unresized = ProcessedImage(
        ok=True,
        data="abc",
        mime_type="image/png",
        original_width=100,
        original_height=100,
        width=100,
        height=100,
        was_resized=False,
    )
    assert format_dimension_note(unresized) is None

    resized = ProcessedImage(
        ok=True,
        data="abc",
        mime_type="image/jpeg",
        original_width=3000,
        original_height=1500,
        width=2000,
        height=1000,
        was_resized=True,
    )
    assert format_dimension_note(resized) == (
        "[Image: original 3000x1500, displayed at 2000x1000. Multiply coordinates by 1.50 to map to original image.]"
    )


def test_process_image_corrupted_bytes_fails_gracefully() -> None:
    res = process_image(b"not a valid image", "image/png")
    assert res.ok is False
    assert "could not be processed" in res.error.lower() or "error" in res.error.lower()
