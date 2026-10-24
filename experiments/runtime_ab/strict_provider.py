"""严格 Provider：模拟主流大模型服务端对消息序列的契约校验。

真实 provider（OpenAI / Anthropic / DeepSeek）会拒绝以下转录本并返回 400：

1. assistant 声明了 ``tool_calls``，后面却没有对应的 ``tool`` 消息；
2. 出现找不到来源的 ``tool`` 消息（孤儿结果）；
3. assistant 消息既无正文也无工具调用（空失败轮）。

本模块只做校验，不产生真实网络请求，用于故障注入实验。
"""

from __future__ import annotations

from typing import Any

from my_agent_llm.models import Message, Response, StreamChunk


class ProviderContractError(Exception):
    """模拟 provider 返回 400 的契约错误。"""

    code = 400

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("400 invalid_request: " + "; ".join(problems))


def _tool_calls(message: Message) -> list[dict[str, Any]]:
    if message.role != "assistant" or not message.metadata:
        return []
    calls = message.metadata.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [c for c in calls if isinstance(c, dict)]


def validate_transcript(messages: list[Message] | tuple[Message, ...]) -> list[str]:
    """按 OpenAI 的 tool-call 契约校验转录本，返回问题列表（空列表 = 合法）。"""
    problems: list[str] = []
    pending: set[str] = set()

    for idx, message in enumerate(messages):
        if message.role == "assistant":
            if pending:
                problems.append(f"message[{idx}] 之前仍有未闭合的 tool_calls: {sorted(pending)}")
                pending = set()
            calls = _tool_calls(message)
            if calls:
                ids = {str(c.get("id") or "") for c in calls}
                if "" in ids:
                    problems.append(f"message[{idx}] 的 tool_call 缺少 id")
                pending |= ids
            elif not message.text_content.strip():
                problems.append(f"message[{idx}] 是既无正文也无 tool_calls 的空 assistant 轮")
        elif message.role == "tool":
            tool_call_id = str((message.metadata or {}).get("tool_call_id") or "")
            if tool_call_id in pending:
                pending.discard(tool_call_id)
            else:
                problems.append(f"message[{idx}] 是孤儿 tool 结果: {tool_call_id!r}")

    if pending:
        problems.append(f"转录本末尾仍有未闭合的 tool_calls: {sorted(pending)}")

    return problems


class StrictProvider:
    """校验通过才返回固定回答的假 Provider。"""

    def __init__(self, final_text: str = "done") -> None:
        self.final_text = final_text
        self.calls = 0
        self.rejected = False
        self.config = None

    def _check(self, messages) -> None:
        self.calls += 1
        problems = validate_transcript(messages)
        if problems:
            self.rejected = True
            raise ProviderContractError(problems)

    async def achat(self, messages, tools=None, **kwargs) -> Response:
        self._check(messages)
        return Response(content=self.final_text, model="strict-fake")

    async def achat_stream(self, messages, tools=None, **kwargs):
        response = await self.achat(messages, tools=tools, **kwargs)
        yield StreamChunk(content=response.content)
