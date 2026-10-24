from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

from my_agent_core.session import Session
from my_agent_core.session.assets import load_image_asset, save_image_asset
from my_agent_llm.config import Config
from my_agent_llm.providers.openai import OpenAIProvider


def _create_base64_image(width: int = 150, height: int = 100, color: str = "green") -> str:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_save_and_load_image_asset(tmp_path: Path) -> None:
    b64_data = _create_base64_image(120, 80)
    meta = save_image_asset(tmp_path, b64_data, "image/png")

    assert meta["type"] == "image"
    assert meta["mime_type"] == "image/png"
    assert "asset_path" in meta
    assert meta["asset_path"].startswith("images/")
    assert meta["width"] == 120
    assert meta["height"] == 80

    asset_file = tmp_path / meta["asset_path"]
    assert asset_file.exists()
    assert asset_file.is_file()

    # 加载恢复
    loaded_b64 = load_image_asset(tmp_path, meta["asset_path"])
    assert loaded_b64 == b64_data


def test_session_spills_image_to_disk_and_keeps_jsonl_lightweight(tmp_path: Path) -> None:
    session_file = tmp_path / "session_test.jsonl"
    session = Session(path=session_file, cwd=tmp_path)

    large_b64 = _create_base64_image(400, 300, color="purple")
    # 添加含图片的多模态消息
    msg_content = [
        {"type": "text", "text": "Check this screenshot"},
        {"type": "image", "data": large_b64, "mime_type": "image/png"},
    ]
    session.add_message(role="user", content=msg_content)

    # 验证 JSONL 文件内容：Base64 必须已被剔除，只留 asset_path
    jsonl_text = session_file.read_text(encoding="utf-8")
    assert large_b64 not in jsonl_text
    assert "images/" in jsonl_text
    assert "asset_path" in jsonl_text

    # 验证资产文件实际落盘
    images_dir = tmp_path / "images"
    assert images_dir.exists()
    image_files = list(images_dir.glob("*.png"))
    assert len(image_files) == 1


def test_session_load_and_transparent_hydration(tmp_path: Path) -> None:
    session_file = tmp_path / "session_hydrate.jsonl"
    session = Session(path=session_file, cwd=tmp_path)

    orig_b64 = _create_base64_image(200, 150, color="yellow")
    session.add_message(
        role="user",
        content=[
            {"type": "text", "text": "Look at this"},
            {"type": "image", "data": orig_b64, "mime_type": "image/png"},
        ],
    )

    # 重新加载 Session
    reloaded_session = Session.load(session_file)
    messages = reloaded_session.get_current_path_messages()
    assert len(messages) == 1

    user_msg = messages[0]
    assert isinstance(user_msg.content, list)
    image_part = next(p for p in user_msg.content if p.get("type") == "image")

    # 验证 data 已透明水合
    assert image_part.get("data") == orig_b64

    # 验证向 Provider 发送时正常生成 Payload
    provider = OpenAIProvider(Config(model="gpt-4o", api_key="sk-fake"))
    payload = provider._convert_messages([user_msg])[0]
    assert payload["role"] == "user"
    assert isinstance(payload["content"], list)
    img_block = next(b for b in payload["content"] if b.get("type") == "image_url")
    assert img_block["image_url"]["url"].startswith("data:image/png;base64,")
