from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from my_agent_core.tools import ToolResult
from my_coding_agent.tools.read import make_read_tool


def _create_image_file(path: Path, width: int = 200, height: int = 150, fmt: str = "PNG") -> None:
    img = Image.new("RGB", (width, height), color="red")
    img.save(path, format=fmt)


@pytest.mark.anyio
async def test_read_tool_reads_png_file_returns_multimodal(tmp_path: Path) -> None:
    img_path = tmp_path / "screenshot.png"
    _create_image_file(img_path, 300, 200, "PNG")

    read_tool = make_read_tool(tmp_path)
    result = await read_tool.func(path="screenshot.png")

    assert isinstance(result, list)
    assert len(result) == 2

    text_block = result[0]
    assert text_block["type"] == "text"
    assert "Read image file [image/png]" in text_block["text"]

    image_block = result[1]
    assert image_block["type"] == "image"
    assert image_block["mime_type"] == "image/png"
    assert len(image_block["data"]) > 0


@pytest.mark.anyio
async def test_read_tool_reads_jpg_file(tmp_path: Path) -> None:
    img_path = tmp_path / "photo.jpg"
    _create_image_file(img_path, 100, 100, "JPEG")

    read_tool = make_read_tool(tmp_path)
    result = await read_tool.func(path="photo.jpg")

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["type"] == "text"
    assert "Read image file [image/jpeg]" in result[0]["text"]
    assert result[1]["type"] == "image"
    assert result[1]["mime_type"] == "image/jpeg"


def test_tool_result_preserves_multimodal_content() -> None:
    multimodal_blocks = [
        {"type": "text", "text": "Read image file [image/png]"},
        {"type": "image", "data": "A" * 10000, "mime_type": "image/png"},
    ]
    res = ToolResult(ok=True, data=multimodal_blocks)
    serialized = res.serialize()

    # 必须保持结构体完整，绝不能被当作普通纯文本切碎或截断
    assert isinstance(serialized, list)
    assert serialized == multimodal_blocks


@pytest.mark.anyio
async def test_read_tool_rejects_non_image_binary(tmp_path: Path) -> None:
    bin_path = tmp_path / "unknown.bin"
    bin_path.write_bytes(b"\x00\x01\x02\x03\xff\xfe" * 50)

    read_tool = make_read_tool(tmp_path)
    result = await read_tool.func(path="unknown.bin")

    assert isinstance(result, str)
    assert "Cannot read binary file" in result
