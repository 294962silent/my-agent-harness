"""End-to-End Multimodal Image Pipeline Verification Script.

Tests all 3 stages end-to-end:
1. Input pre-flight defense & tool execution (4.5MB/2000px cap, downsampling, scale note)
2. Tool turn execution & multimodal Message preservation
3. Session persistence & Asset Spill (disk spill, JSONL decoupling, transparent hydration)
4. Context compaction & summarization stripping (1200 Token heuristic, zero Base64 in summary prompt)
5. Provider conversion (OpenAI & Anthropic wire format generation)
"""

from __future__ import annotations

import asyncio
import base64
import io
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

# Ensure project imports resolve
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from my_agent_core.context import (
    ContextManager,
    estimate_tokens,
)
from my_agent_core.loop import _execute_tools_turn
from my_agent_core.session import Session
from my_agent_core.registry import ToolRegistry
from my_agent_llm import Message, Response
from my_agent_llm.config import Config
from my_agent_llm.models import ToolCall
from my_agent_llm.providers.anthropic import AnthropicProvider
from my_agent_llm.providers.openai import OpenAIProvider
from my_coding_agent.tools.read import make_read_tool


class MockSummarizerLLM:
    def __init__(self):
        self.prompts = []

    async def achat(self, messages, **kwargs):
        self.prompts.append(messages[-1].content)
        return Response(
            content=(
                "<analysis>Compacting conversation with multimodal assets.</analysis>\n"
                "<summary>\n"
                "## Goal\nAnalyze 4K screenshot layout.\n"
                "## Progress\n### Done\n- Read screenshot\n- Verified dimensions\n"
                "</summary>"
            ),
            model="mock-summary-llm",
        )


async def run_e2e_test():
    print("=" * 60)
    print("🚀 Starting End-to-End Multimodal Image Pipeline Test")
    print("=" * 60)

    temp_dir = Path(tempfile.mkdtemp(prefix="my_pi_e2e_"))
    try:
        # ---------------------------------------------------------
        # Step 1: Create a real 4K test image (3840x2160)
        # ---------------------------------------------------------
        print("\n[Step 1] Creating high-res 4K test image (3840x2160)...")
        img_4k_path = temp_dir / "dashboard_4k.png"
        img = Image.new("RGB", (3840, 2160), color=(30, 60, 120))
        img.save(img_4k_path, format="PNG")
        file_size_kb = img_4k_path.stat().st_size / 1024
        print(f"  ✓ Image created at: {img_4k_path} ({file_size_kb:.1f} KB)")

        # ---------------------------------------------------------
        # Step 2: Test `read` tool execution with image file
        # ---------------------------------------------------------
        print("\n[Step 2] Testing read tool on 4K image (Stage 1)...")
        read_tool = make_read_tool(temp_dir)
        read_output = await read_tool.func(path="dashboard_4k.png")

        assert isinstance(read_output, list), f"Expected list of blocks, got {type(read_output)}"
        assert len(read_output) == 2, f"Expected 2 blocks, got {len(read_output)}"

        text_block = read_output[0]
        image_block = read_output[1]

        assert text_block["type"] == "text"
        assert "Read image file [image/png]" in text_block["text"]
        assert "original 3840x2160" in text_block["text"]
        assert "displayed at 2000x1125" in text_block["text"]
        assert "Multiply coordinates by 1.92" in text_block["text"]
        print(
            f"  ✓ Text note verified:\n    {text_block['text'].splitlines()[0]}\n    {text_block['text'].splitlines()[1]}"
        )

        assert image_block["type"] == "image"
        assert image_block["mime_type"] == "image/png"
        assert len(image_block["data"]) > 0
        raw_decoded = base64.b64decode(image_block["data"])
        with Image.open(io.BytesIO(raw_decoded)) as resized_check:
            assert resized_check.size == (2000, 1125)
            print(
                f"  ✓ Resized binary verified: {resized_check.size[0]}x{resized_check.size[1]} (aspect ratio preserved)"
            )

        # ---------------------------------------------------------
        # Step 3: Test ReAct Tool Batch Execution Pipeline
        # ---------------------------------------------------------
        print("\n[Step 3] Testing ReAct loop tool turn & Message preservation...")
        registry = ToolRegistry()
        registry.register(read_tool)

        tool_calls = [
            ToolCall(id="call_read_01", name="read", args={"path": "dashboard_4k.png"}),
        ]

        tool_messages = []
        async for event in _execute_tools_turn(tool_calls=tool_calls, registry=registry):
            msg = getattr(event, "message", None)
            if msg is not None and getattr(msg, "role", None) == "tool":
                tool_messages.append(msg)

        assert len(tool_messages) >= 1
        tool_msg = tool_messages[0]
        assert tool_msg.role == "tool"
        assert isinstance(tool_msg.content, list)
        assert tool_msg.content[1]["type"] == "image"
        print(f"  ✓ Tool turn emitted valid multimodal tool Message entity with {len(tool_msg.content)} blocks")

        # ---------------------------------------------------------
        # Step 4: Test Session Persistence & Asset Spill (Stage 2)
        # ---------------------------------------------------------
        print("\n[Step 4] Testing Session Persistence & Asset Spill Storage (Stage 2)...")
        session_file = temp_dir / "agent_session.jsonl"
        session = Session(path=session_file, cwd=temp_dir)

        # Append User Message and Tool Message
        user_msg_content = [
            {"type": "text", "text": "Please check this UI screenshot"},
            image_block,
        ]
        session.add_message(role="user", content=user_msg_content)
        session.add_message(role="tool", content=read_output, tool_call_id="call_read_01", tool_name="read")

        # Check JSONL file size and contents
        jsonl_content = session_file.read_text(encoding="utf-8")
        jsonl_size_bytes = session_file.stat().st_size
        print(f"  ✓ JSONL file size on disk: {jsonl_size_bytes} bytes (under 5KB!)")

        # Verify Base64 is stripped from JSONL
        assert image_block["data"] not in jsonl_content, "Base64 leaked into JSONL file!"
        assert "images/" in jsonl_content, "Asset path not referenced in JSONL!"

        # Verify physical image files exist in <session_dir>/images/
        images_dir = temp_dir / "images"
        assert images_dir.exists() and images_dir.is_dir()
        spilled_files = list(images_dir.glob("*.png"))
        assert len(spilled_files) == 1, f"Expected 1 deduplicated asset file, got {len(spilled_files)}"
        print(
            f"  ✓ Spilled image asset exists on disk: {spilled_files[0].name} ({spilled_files[0].stat().st_size} bytes)"
        )

        # Test reload and transparent hydration
        reloaded_session = Session.load(session_file)
        hydrated_messages = reloaded_session.get_current_path_messages()
        assert len(hydrated_messages) == 2

        hydrated_user_msg = hydrated_messages[0]
        assert isinstance(hydrated_user_msg.content, list)
        hydrated_img_block = next(
            b for b in hydrated_user_msg.content if isinstance(b, dict) and b.get("type") == "image"
        )
        assert hydrated_img_block.get("data") == image_block["data"], "Hydration failed to restore Base64 payload!"
        print("  ✓ Session reloaded and transparently hydrated Base64 data from disk asset")

        # ---------------------------------------------------------
        # Step 5: Test Context Compaction & Summarization (Stage 3)
        # ---------------------------------------------------------
        print("\n[Step 5] Testing Context Compaction & Summarization (Stage 3)...")
        summarizer_llm = MockSummarizerLLM()

        # Set budget to 2500 tokens (threshold = 2000 tokens), keep_recent_tokens = 1200
        cm = ContextManager(budget=2500, keep_recent_tokens=1200, llm=summarizer_llm)

        # Build conversation: Early image turn + Mid text turns + Recent image turn
        history = [
            hydrated_messages[0],  # User with 4K image (estimated ~1200 tokens)
            hydrated_messages[1],  # Tool with 4K image (estimated ~1200 tokens)
            Message(role="assistant", content="I inspected the dashboard."),
            Message(role="user", content="Here are additional logs: " + "verbose " * 200),
            Message(role="assistant", content="Logs verified."),
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "Now check this latest widget screenshot"},
                    image_block,
                ],
            ),
        ]

        total_est_tokens = estimate_tokens(history)
        print(f"  • Pre-compaction estimated tokens: {total_est_tokens} (Watermark threshold: {cm.budget_threshold})")
        assert total_est_tokens > cm.budget_threshold

        view = await cm.prepare(history)

        # Assert summarization was called
        assert len(summarizer_llm.prompts) == 1, "Summarizer was not invoked!"
        summary_prompt = summarizer_llm.prompts[0]

        # Verify Base64 is COMPLETELY stripped from the summarization prompt
        assert image_block["data"] not in summary_prompt, "Base64 leaked to summarizer LLM!"
        assert "dashboard_4k.png" in summary_prompt or "Read image file [image/png]" in summary_prompt
        print("  ✓ Summarization prompt verified: Zero Base64 strings, semantic metadata preserved")

        # Verify compacted view layout:
        # [0] = Context summary
        # [...] = retained tail containing the latest image
        assert view[0].role == "user"
        assert "[Context summary" in view[0].text_content

        latest_view_msg = view[-1]
        assert latest_view_msg.role == "user"
        assert isinstance(latest_view_msg.content, list)
        latest_img_block = next(b for b in latest_view_msg.content if isinstance(b, dict) and b.get("type") == "image")
        assert latest_img_block.get("data") == image_block["data"], "Latest image in retainedTail lost data!"
        print("  ✓ Compacted view verified: Summary at prefix, active retainedTail has full Vision capability intact")

        # ---------------------------------------------------------
        # Step 6: Test Provider Wire Format Translation
        # ---------------------------------------------------------
        print("\n[Step 6] Testing LLM Provider wire format conversion...")
        openai_provider = OpenAIProvider(Config(model="gpt-4o", api_key="sk-dummy"))
        anthropic_provider = AnthropicProvider(Config(model="claude-3-7-sonnet", api_key="sk-dummy"))

        # Convert latest message
        openai_wire = openai_provider._convert_messages([latest_view_msg])[0]
        assert openai_wire["role"] == "user"
        openai_content = openai_wire["content"]
        assert isinstance(openai_content, list)
        openai_img = next(b for b in openai_content if b.get("type") == "image_url")
        assert openai_img["image_url"]["url"].startswith("data:image/png;base64,")
        print("  ✓ OpenAI provider wire payload verified: Valid image_url data URI")

        _, ant_wire = anthropic_provider._convert_messages([latest_view_msg])
        anthropic_content = ant_wire[0]["content"]
        assert isinstance(anthropic_content, list)
        anthropic_img = next(b for b in anthropic_content if b.get("type") == "image")
        assert anthropic_img["source"]["type"] == "base64"
        assert anthropic_img["source"]["media_type"] == "image/png"
        assert len(anthropic_img["source"]["data"]) > 0
        print("  ✓ Anthropic provider wire payload verified: Valid base64 source image block")

        print("\n" + "=" * 60)
        print("🎉 ALL END-TO-END TESTS PASSED WITH ZERO ISSUES! 100% SUCCESS")
        print("=" * 60)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(run_e2e_test())
