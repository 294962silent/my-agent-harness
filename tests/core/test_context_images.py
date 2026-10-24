from __future__ import annotations

import pytest

from my_agent_core.context import (
    IMAGE_EQUIVALENT_CHARS,
    ContextManager,
    _serialize_messages,
    estimate_tokens,
)
from my_agent_llm import Message, Response
from my_agent_llm.models import ToolCall


class FakeLLM:
    def __init__(self, reply: str = "<analysis>analysis</analysis>\n<summary>summary</summary>"):
        self.reply = reply
        self.calls: list[dict] = []

    async def achat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return Response(content=self.reply, model="fake-summarizer")


def test_image_token_estimation_alignment() -> None:
    # 验证等效字符数为 4800（对标 Pi 的 ESTIMATED_IMAGE_CHARS = 4800, 即 1200 Tokens）
    assert IMAGE_EQUIVALENT_CHARS == 4800

    msg = Message(
        role="user",
        content=[
            {"type": "text", "text": "a" * 400},
            {"type": "image", "data": "LONG_BASE64_PAYLOAD" * 500, "mime_type": "image/png"},
        ],
    )
    tokens = estimate_tokens([msg])
    # 400 字符 + 4800 等效字符 = 5200 字符 -> 5200 // 4 = 1300 tokens (排除 base64 膨胀)
    # base_chars 会有少量 json 字段名开销，估算值应在 ~1300 附近
    assert 1300 <= tokens <= 1350


def test_summarize_serialization_strips_images() -> None:
    messages = [
        Message(
            role="user",
            content=[
                {"type": "text", "text": "Inspect the UI layout"},
                {"type": "image", "data": "SECRET_BASE64_IMAGE_DATA_12345", "mime_type": "image/png"},
            ],
        ),
        Message(
            role="assistant",
            content="",
            metadata={"tool_calls": [ToolCall(id="c1", name="read", args={"path": "ui.png"}).model_dump()]},
        ),
        Message(
            role="tool",
            content=[
                {"type": "text", "text": "Read image file [image/png]\n[Image: original 1920x1080]"},
                {"type": "image", "data": "SECRET_BASE64_IMAGE_DATA_67890", "mime_type": "image/png"},
            ],
            metadata={"tool_call_id": "c1", "tool_name": "read"},
        ),
    ]

    serialized = _serialize_messages(messages)

    # 1. 业务文本与元数据必须保留
    assert "user: Inspect the UI layout" in serialized
    assert "assistant: [tool_calls: read]" in serialized
    assert "Read image file [image/png]" in serialized
    assert "[Image: original 1920x1080]" in serialized

    # 2. Base64 字符串必须彻底被剥离，仅保留轻量 [image] 标记
    assert "SECRET_BASE64_IMAGE_DATA_12345" not in serialized
    assert "SECRET_BASE64_IMAGE_DATA_67890" not in serialized
    assert "[image]" in serialized


@pytest.mark.anyio
async def test_context_compaction_preserves_retained_tail_images() -> None:
    # 预算设为 3000 tokens，阈值 2400 tokens，keep_recent_tokens = 1500
    llm = FakeLLM("<analysis>analyzed</analysis>\n<summary>\n## Goal\nTest\n## Progress\nDone\n</summary>")
    cm = ContextManager(budget=3000, keep_recent_tokens=1500, llm=llm)

    # 构造历史：早期的图片消息 + 中期对话 + 最新的图片消息
    early_img_msg = Message(
        role="user",
        content=[
            {"type": "text", "text": "Early message with image"},
            {"type": "image", "data": "EARLY_IMAGE_BASE64", "mime_type": "image/png"},
        ],
    )
    mid_msg_1 = Message(role="assistant", content="Acknowledged early image")
    mid_msg_2 = Message(role="user", content="Here is a lot of text: " + "word " * 500)
    mid_msg_3 = Message(role="assistant", content="Processed text: " + "word " * 500)
    latest_img_msg = Message(
        role="user",
        content=[
            {"type": "text", "text": "Latest message with image"},
            {"type": "image", "data": "LATEST_IMAGE_BASE64", "mime_type": "image/png"},
        ],
    )

    history = [early_img_msg, mid_msg_1, mid_msg_2, mid_msg_3, latest_img_msg]
    view = await cm.prepare(history)

    # 触发压缩后：
    # 1. 摘要模型被调用
    assert len(llm.calls) == 1
    summary_prompt = llm.calls[0]["messages"][-1].content
    assert "Early message with image" in summary_prompt
    assert "EARLY_IMAGE_BASE64" not in summary_prompt  # 摘要时图片已被剥离

    # 2. 返回视图首部包含摘要
    assert view[0].role == "user"
    assert "Context summary" in view[0].text_content

    # 3. 最新尾部消息保留在活跃视窗中，且图片数据完好无损
    last_msg = view[-1]
    assert last_msg.role == "user"
    assert isinstance(last_msg.content, list)
    latest_img = next(p for p in last_msg.content if p.get("type") == "image")
    assert latest_img.get("data") == "LATEST_IMAGE_BASE64"
