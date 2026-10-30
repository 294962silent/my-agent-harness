"""预算受限策略：给 agent 加"步数预算"，制造真实失败分布。

动机：当前模型在 100 个任务上全部成功，无法产生偏好信号。
真实工程里 agent 经常在**预算耗尽**时失败（token 上限、时间上限、轮次上限），
这也是 RL 训练最需要覆盖的失败模式。

实现：包一层 LLM 客户端，在累计工具调用超过预算后**拒绝服务**（返回终止回复），
让任务以失败收场。这模拟"预算耗尽的 agent"，与单次策略共用同一套判定。
"""

from __future__ import annotations

from typing import Any

from experiments.agent_rl.runner import Episode, run_task_once
from experiments.agent_rl.task import Task

BUDGET_PROMPT = """You have a strict budget of {budget} tool calls. Plan accordingly:
inspect only the files you need. If you exceed the budget the task fails.
"""


class BudgetExhaustedClient:
    """包装真实客户端：工具调用计数超过预算后，模型不再产出有效行动。

    实现方式：拦在 chat 层面——超过预算后，返回一条"我放弃"的固定回复，
    于是 agent 自然终止，任务判定失败。这样失败是**由预算导致**，可复现。
    """

    def __init__(self, inner: Any, budget: int) -> None:
        self._inner = inner
        self.budget = budget
        self.calls = 0
        self.exhausted = False

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "unknown")

    def _burned(self) -> bool:
        """粗粒度计量：每次模型调用视为一次"预算消耗"。"""
        self.calls += 1
        if self.calls > self.budget:
            self.exhausted = True
            return True
        return False

    async def achat(self, messages, tools=None, model=None):
        if self._burned():
            return _give_up_response(self.model)
        return await self._inner.achat(messages, tools=tools, model=model)

    async def achat_stream(self, messages, tools=None, model=None):
        if self._burned():
            response = _give_up_response(self.model)
            from my_agent_llm import StreamChunk

            yield StreamChunk(content=response.content)
            return
        async for chunk in self._inner.achat_stream(messages, tools=tools, model=model):
            yield chunk


def _give_up_response(model: str):
    from my_agent_llm import Response

    return Response(
        content="I ran out of budget and cannot continue this task.",
        model=model,
    )


async def run_budget_limited(
    task: Task,
    client_factory,
    budget: int,
    sample_index: int = 0,
) -> Episode:
    """在预算约束下跑一次任务。"""
    inner = client_factory()
    client = BudgetExhaustedClient(inner, budget=budget)
    episode = await run_task_once(task, model_client=client, sample_index=sample_index)
    episode.error = "budget_exhausted" if client.exhausted else episode.error
    return episode
