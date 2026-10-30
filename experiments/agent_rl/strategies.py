"""多种 agent 策略的对比实现。

四种策略（共用同一个沙盒与判定）：

1. **single**：单次 ReAct（baseline）。
2. **best_of_k**：独立采样 k 次，用**规则奖励**（判定通过 + 步数更少）挑最优——
   回答"采样能不能救回失败"。
3. **reflexion**：失败后把判定输出（pytest 报错）喂回模型再试，最多 n 轮——
   回答"反馈信号有没有用"。
4. **plan_first**：先让模型输出一个简短计划，再带着计划执行——
   回答"显式规划是否提升稳定性"。

这些策略只在**提示词与调用次数**上有差别，判定逻辑完全一致，因此可比。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from experiments.agent_rl.runner import Episode, run_task_once
from experiments.agent_rl.task import Task

PLAN_PROMPT = """Before you start, write a short plan (3-6 bullet points) for how you will
inspect the repository and fix/implement the task. Then execute it step by step.
"""

RETRY_PROMPT = """Your previous attempt failed verification. The checker reported:

{feedback}

Analyze the failure, then fix the repository so the check passes.
"""


@dataclass
class StrategyResult:
    """一个策略在一个任务上的结果。"""

    task_id: str
    strategy: str
    success: bool
    attempts: int
    reward: float
    episodes: list[Episode] = field(default_factory=list)
    duration_s: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        best = max(self.episodes, key=lambda e: e.reward) if self.episodes else None
        return {
            "task_id": self.task_id,
            "strategy": self.strategy,
            "success": self.success,
            "attempts": self.attempts,
            "reward": self.reward,
            "duration_s": round(self.duration_s, 2),
            "steps_total": sum(e.steps for e in self.episodes),
            "tokens_in": sum(e.tokens_in for e in self.episodes),
            "tokens_out": sum(e.tokens_out for e in self.episodes),
            "best_steps": best.steps if best else 0,
            "error": self.error,
        }


async def run_single(task: Task, client_factory, sample_index: int = 0) -> StrategyResult:
    started = time.time()
    client = client_factory()
    episode = await run_task_once(task, model_client=client, sample_index=sample_index)
    return StrategyResult(
        task_id=task.task_id,
        strategy="single",
        success=episode.success,
        attempts=1,
        reward=episode.reward,
        episodes=[episode],
        duration_s=time.time() - started,
        error=episode.error,
    )


async def run_best_of_k(task: Task, client_factory, k: int = 3) -> StrategyResult:
    """独立采样 k 次，取最优：成功优先，其次步数少。"""
    started = time.time()
    episodes: list[Episode] = []
    for index in range(k):
        client = client_factory()
        episodes.append(await run_task_once(task, model_client=client, sample_index=index))

    def score(episode: Episode) -> tuple[int, int]:
        return (1 if episode.success else 0, -episode.steps)

    best = max(episodes, key=score)
    return StrategyResult(
        task_id=task.task_id,
        strategy=f"best_of_{k}",
        success=best.success,
        attempts=k,
        reward=best.reward,
        episodes=episodes,
        duration_s=time.time() - started,
    )


async def run_reflexion(task: Task, client_factory, max_rounds: int = 2) -> StrategyResult:
    """失败后带着判定反馈重试。"""
    started = time.time()
    episodes: list[Episode] = []
    prompt = task.prompt
    for round_index in range(max_rounds + 1):
        client = client_factory()
        attempt_task = Task(
            task_id=task.task_id,
            template=task.template,
            difficulty=task.difficulty,
            prompt=prompt,
            files=task.files,
            check_cmd=task.check_cmd,
            timeout_s=task.timeout_s,
            meta=task.meta,
        )
        episode = await run_task_once(attempt_task, model_client=client, sample_index=round_index)
        episodes.append(episode)
        if episode.success:
            break
        feedback = episode.check_tail or "(no output)"
        prompt = task.prompt + "\n\n" + RETRY_PROMPT.format(feedback=feedback[:600])

    best = next((e for e in episodes if e.success), episodes[-1])
    return StrategyResult(
        task_id=task.task_id,
        strategy="reflexion",
        success=any(e.success for e in episodes),
        attempts=len(episodes),
        reward=best.reward,
        episodes=episodes,
        duration_s=time.time() - started,
    )


async def run_plan_first(task: Task, client_factory) -> StrategyResult:
    """先规划后执行。"""
    started = time.time()
    client = client_factory()
    planned = Task(
        task_id=task.task_id,
        template=task.template,
        difficulty=task.difficulty,
        prompt=task.prompt + "\n\n" + PLAN_PROMPT,
        files=task.files,
        check_cmd=task.check_cmd,
        timeout_s=task.timeout_s,
        meta={**task.meta, "strategy": "plan_first"},
    )
    episode = await run_task_once(planned, model_client=client, sample_index=0)
    return StrategyResult(
        task_id=task.task_id,
        strategy="plan_first",
        success=episode.success,
        attempts=1,
        reward=episode.reward,
        episodes=[episode],
        duration_s=time.time() - started,
        error=episode.error,
    )




async def run_budget(task: Task, client_factory, budget: int = 4) -> StrategyResult:
    """预算受限：限制模型调用次数，模拟"预算耗尽的 agent"。"""
    started = time.time()
    from experiments.agent_rl.budget import run_budget_limited

    episode = await run_budget_limited(task, client_factory, budget=budget)
    return StrategyResult(
        task_id=task.task_id,
        strategy=f"budget_{budget}",
        success=episode.success,
        attempts=1,
        reward=episode.reward,
        episodes=[episode],
        duration_s=time.time() - started,
        error=episode.error,
    )




STRATEGIES = {
    "single": lambda task, factory: run_single(task, factory),
    "best_of_3": lambda task, factory: run_best_of_k(task, factory, k=3),
    "reflexion": lambda task, factory: run_reflexion(task, factory, max_rounds=2),
    "plan_first": lambda task, factory: run_plan_first(task, factory),
}
STRATEGIES.update({
    "budget_2": lambda task, factory: run_budget(task, factory, budget=2),
    "budget_4": lambda task, factory: run_budget(task, factory, budget=4),
    "budget_8": lambda task, factory: run_budget(task, factory, budget=8),
})
