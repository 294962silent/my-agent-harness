# pyright: reportAttributeAccessIssue=false
"""OpenAIProvider 翻译测试（假 SDK）。"""

from types import SimpleNamespace

import openai
import pytest
from my_agent_llm.config import Config
from my_agent_llm.models import Message, Response, ToolCall
from my_agent_llm.providers.openai import OpenAIProvider
from tests.llm.fakes import FakeOpenAI, make_bad_request_error, make_openai_response


def _provider(responses):
    return OpenAIProvider(Config(api_key="test"), client=FakeOpenAI(responses))


class _FakeSyncStream:
    """同步流替身：__iter__ 按脚本产块。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)


def _text_chunk(content, finish=None):
    return SimpleNamespace(
        id="c",
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content), finish_reason=finish)],
        usage=None,
    )


def _usage_only_chunk(prompt=10, completion=5):
    return SimpleNamespace(
        id="u",
        choices=[],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion),
    )


def test_chat_returns_response():
    """chat → Response：content/model/usage/finish_reason。"""
    p = _provider([make_openai_response(content="hello")])
    resp = p.chat([Message(role="user", content="hi")], model="gpt-4.1-mini")
    assert isinstance(resp, Response)
    assert resp.content == "hello"
    assert resp.model == "gpt-4.1-mini"
    assert resp.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert resp.finish_reason == "stop"


def test_chat_passes_messages_and_tools():
    """chat → SDK 收到正确 messages/tools。"""
    p = _provider([make_openai_response()])
    tools = [
        {
            "type": "function",
            "function": {"name": "f", "description": "", "parameters": {}},
        }
    ]
    p.chat(
        [Message(role="user", content="hi")],
        model="gpt-4.1-mini",
        tools=tools,
        temperature=0.5,
    )
    call = p.client.calls[0]
    assert call["model"] == "gpt-4.1-mini"
    assert call["messages"] == [{"role": "user", "content": "hi"}]
    assert call["tools"] == tools
    assert call["temperature"] == 0.5


def test_chat_converts_tool_messages():
    """tool 消息 → wire dict 带 tool_call_id。"""
    p = _provider([make_openai_response()])
    p.chat(
        [
            Message(
                role="assistant",
                content="",
                metadata={
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ]
                },
            ),
            Message(role="tool", content="result", metadata={"tool_call_id": "1"}),
        ],
        model="gpt-4.1-mini",
    )
    call = p.client.calls[0]
    assert call["messages"][0] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "1",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ],
    }
    assert call["messages"][1] == {
        "role": "tool",
        "content": "result",
        "tool_call_id": "1",
    }


def test_chat_extracts_tool_calls():
    """响应 tool_calls → Response.tool_calls（统一结构化实体）。"""
    tc = [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    p = _provider([make_openai_response(tool_calls=tc)])
    resp = p.chat([Message(role="user", content="hi")], model="gpt-4.1-mini")
    assert resp.tool_calls == [ToolCall(id="1", name="f", args={})]


def test_stream_yields_chunks():
    """stream → 文本增量序列；usage-only 末块（choices 为空）跳过。"""

    class FakeStream:
        def __init__(self):
            self.chunks = [
                SimpleNamespace(
                    id="1",
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="a"), finish_reason=None)],
                ),
                SimpleNamespace(
                    id="2",
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="b"), finish_reason="stop")],
                ),
                SimpleNamespace(id="3", choices=[]),  # usage-only 末块
            ]

        def __iter__(self):
            return iter(self.chunks)

    p = _provider([])
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: FakeStream())))
    chunks = list(p.stream([Message(role="user", content="hi")], model="gpt-4.1-mini"))
    assert [c.content for c in chunks] == ["a", "b", ""]
    assert chunks[-1].response is not None
    assert chunks[-1].response.content == "ab"


def test_stream_aggregates_tool_calls():
    """流式增量 tool_calls → 末块聚合完整（id/name 来一次、arguments 碎片拼接）。"""

    class FakeStream:
        def __init__(self):
            self.chunks = [
                # 工具调用增量片段：index 0，id + name 只出现在第一片
                SimpleNamespace(
                    id="1",
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_1",
                                        function=SimpleNamespace(name="get_weather", arguments=""),
                                    )
                                ],
                            ),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    id="2",
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id=None,
                                        function=SimpleNamespace(name=None, arguments='{"city":'),
                                    )
                                ],
                            ),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    id="3",
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id=None,
                                        function=SimpleNamespace(name=None, arguments='"Tokyo"}'),
                                    )
                                ],
                            ),
                            finish_reason="tool_calls",
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    id="4",
                    choices=[],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                ),
            ]

        def __iter__(self):
            return iter(self.chunks)

    p = _provider([])
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: FakeStream())))
    chunks = list(p.stream([Message(role="user", content="hi")], model="gpt-4.1-mini"))
    # 无文本增量（content=None），只剩一个聚合末块
    assert [c.content for c in chunks] == [""]
    assert chunks[0].tool_calls == [ToolCall(id="call_1", name="get_weather", args={"city": "Tokyo"})]
    assert chunks[0].usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert chunks[0].finish_reason == "tool_calls"  # 末块透传循环内捕获的 finish_reason


def test_convert_messages_skips_empty_tool_calls():
    """assistant 消息包含空的 tool_calls: [] 时，不产生 tool_calls 键（防 400）。"""
    p = _provider([make_openai_response()])
    p.chat(
        [
            Message(
                role="assistant",
                content="done",
                metadata={"tool_calls": []},
            ),
        ],
        model="gpt-4.1-mini",
    )
    call = p.client.calls[0]
    assert call["messages"] == [{"role": "assistant", "content": "done"}]
    assert "tool_calls" not in call["messages"][0]


# ── 流式 usage：请求流末尾携带 usage，并兼容不支持该参数的网关 ──────────────


def test_stream_requests_include_usage_and_extracts_tail_usage():
    """流式请求体带 stream_options.include_usage；usage-only 末块被 _extract_usage 取到。"""
    usage_chunk = _usage_only_chunk(10, 5)
    p = _provider([_FakeSyncStream([_text_chunk("a"), _text_chunk("b", "stop"), usage_chunk])])

    chunks = list(p.stream([Message(role="user", content="hi")], model="gpt-4.1-mini"))

    assert p.client.calls[0]["stream_options"] == {"include_usage": True}
    assert p.client.calls[0]["stream"] is True
    # 末块（choices 为空）的 usage 被正确提取，并透传到聚合末块
    assert OpenAIProvider._extract_usage(usage_chunk) == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert [c.content for c in chunks] == ["a", "b", ""]
    assert chunks[-1].usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert chunks[-1].response is not None
    assert chunks[-1].response.usage == chunks[-1].usage


def test_stream_explicit_stream_options_are_respected():
    """调用方显式传 stream_options 时不覆盖。"""
    p = _provider([_FakeSyncStream([_text_chunk("ok", "stop")])])
    list(
        p.stream(
            [Message(role="user", content="hi")],
            model="gpt-4.1-mini",
            stream_options={"include_usage": False},
        )
    )
    assert p.client.calls[0]["stream_options"] == {"include_usage": False}


def test_stream_downgrades_once_when_gateway_rejects_stream_options():
    """网关 400 拒绝 stream_options → 去掉参数重试一次，且后续请求不再尝试。"""
    p = _provider(
        [
            make_bad_request_error("Unsupported parameter: stream_options"),
            _FakeSyncStream([_text_chunk("a", "stop"), _usage_only_chunk(3, 2)]),
            _FakeSyncStream([_text_chunk("b", "stop")]),
        ]
    )

    first = list(p.stream([Message(role="user", content="hi")], model="gpt-4.1-mini"))
    assert [c.content for c in first] == ["a", ""]
    assert first[-1].usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}

    # 只重试一次：两次 create，第一次带参数，第二次不带
    assert len(p.client.calls) == 2
    assert p.client.calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in p.client.calls[1]
    assert p._stream_usage_supported is False

    # 降级被记住：下一次请求直接不带参数，也不再多打一次
    list(p.stream([Message(role="user", content="hi")], model="gpt-4.1-mini"))
    assert len(p.client.calls) == 3
    assert "stream_options" not in p.client.calls[2]


def test_stream_400_unrelated_error_is_not_retried():
    """400 但与 stream_options 无关 → 原样抛出，不重试、不降级。"""
    p = _provider([make_bad_request_error("The model `nope` does not exist")])
    with pytest.raises(openai.BadRequestError):
        list(p.stream([Message(role="user", content="hi")], model="nope"))
    assert len(p.client.calls) == 1
    assert p._stream_usage_supported is True


def test_stream_non_400_error_is_not_retried():
    """非 400 错误（如 500）→ 不触发降级重试。"""
    response = SimpleNamespace(status_code=500, headers={}, request=SimpleNamespace())
    err = openai.InternalServerError("Error code: 500 - upstream boom", response=response, body=None)
    p = _provider([err])
    with pytest.raises(openai.InternalServerError):
        list(p.stream([Message(role="user", content="hi")], model="gpt-4.1-mini"))
    assert len(p.client.calls) == 1
    assert p._stream_usage_supported is True
