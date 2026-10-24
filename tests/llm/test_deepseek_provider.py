# pyright: reportAttributeAccessIssue=false
"""DeepSeekProvider 翻译测试：OpenAI 兼容 + reasoning 提取。"""

from types import SimpleNamespace

from my_agent_llm.config import Config
from my_agent_llm.models import Message, ToolCall
from my_agent_llm.providers.deepseek import DeepSeekProvider
from tests.llm.fakes import FakeOpenAI, make_bad_request_error


def _provider(responses):
    return DeepSeekProvider(Config(api_key="test"), client=FakeOpenAI(responses))


def test_chat_extracts_reasoning():
    """响应 reasoning_content → Response.reasoning_content。"""
    from tests.llm.fakes import make_openai_response

    resp = make_openai_response(content="answer")
    resp.choices[0].message.reasoning_content = "thinking..."
    p = _provider([resp])
    out = p.chat([Message(role="user", content="hi")], model="deepseek-chat")
    assert out.content == "answer"
    assert out.reasoning_content == "thinking..."


def test_default_base_url_deepseek():
    """构造时默认 base_url 指向 deepseek。"""
    p = DeepSeekProvider(Config(api_key="test"))
    assert p.config.base_url == "https://api.deepseek.com"


def test_stream_aggregates_tool_calls_and_reasoning():
    """流式增量 tool_calls + reasoning → 末块聚合完整。"""

    class FakeStream:
        def __init__(self):
            self.chunks = [
                SimpleNamespace(
                    id="1",
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                reasoning_content="think",
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_1",
                                        function=SimpleNamespace(name="f", arguments=""),
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
                                reasoning_content="ing",
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id=None,
                                        function=SimpleNamespace(name=None, arguments="{}"),
                                    )
                                ],
                            ),
                            finish_reason="tool_calls",
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    id="3",
                    choices=[],
                    usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
                ),
            ]

        def __iter__(self):
            return iter(self.chunks)

    p = _provider([])
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: FakeStream())))
    chunks = list(p.stream([Message(role="user", content="hi")], model="deepseek-chat"))
    assert [c.content for c in chunks] == [""]
    last = chunks[0]
    assert last.tool_calls == [ToolCall(id="call_1", name="f", args={})]
    assert last.metadata == {"reasoning_content": "thinking"}
    assert last.usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert last.finish_reason == "tool_calls"  # 末块透传循环内捕获的 finish_reason


class _FakeSyncStream:
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


def test_stream_requests_include_usage():
    """DeepSeek 流式同样请求 stream_options.include_usage，并捕获末块 usage。"""
    usage_chunk = SimpleNamespace(
        id="u",
        choices=[],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=4, total_tokens=11),
    )
    p = _provider([_FakeSyncStream([_text_chunk("hi", "stop"), usage_chunk])])

    chunks = list(p.stream([Message(role="user", content="hi")], model="deepseek-chat"))

    assert p.client.calls[0]["stream_options"] == {"include_usage": True}
    assert [c.content for c in chunks] == ["hi", ""]
    assert chunks[-1].usage == {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}


def test_stream_downgrades_once_when_gateway_rejects_stream_options():
    """DeepSeek 兼容网关 400 拒绝 stream_options → 去参重试一次并记住降级。"""
    p = _provider(
        [
            make_bad_request_error("include_usage is not supported by this gateway"),
            _FakeSyncStream([_text_chunk("ok", "stop")]),
            _FakeSyncStream([_text_chunk("again", "stop")]),
        ]
    )

    first = list(p.stream([Message(role="user", content="hi")], model="deepseek-chat"))
    assert [c.content for c in first] == ["ok", ""]
    assert len(p.client.calls) == 2
    assert p.client.calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in p.client.calls[1]

    list(p.stream([Message(role="user", content="hi")], model="deepseek-chat"))
    assert len(p.client.calls) == 3
    assert "stream_options" not in p.client.calls[2]
