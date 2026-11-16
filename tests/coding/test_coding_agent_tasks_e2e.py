import asyncio
import json
import sys
from pathlib import Path

from my_agent_core.session import Session  # pyright: ignore[reportMissingImports]
from my_agent_core.task_store import TaskStore  # pyright: ignore[reportMissingImports]
from my_agent_llm.models import StreamChunk  # pyright: ignore[reportMissingImports]

from my_coding_agent.agent import CodingAgent  # pyright: ignore[reportMissingImports]


class ScriptedCodingLLM:
    def __init__(self, background_command: str):
        self.background_command = background_command
        self.turns = 0
        self.calls = []

    async def achat_stream(self, messages, tools=None, **kwargs):
        self.turns += 1
        self.calls.append(list(messages))
        if self.turns == 1:
            yield StreamChunk(
                content="Starting background test task.",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "todo",
                            "arguments": json.dumps({"action": "create", "subject": "Run test suite"}),
                        },
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": self.background_command, "run_in_background": True}),
                        },
                    },
                ],
            )
        elif self.turns == 2:
            yield StreamChunk(content="Waiting for background task...")
        else:
            yield StreamChunk(
                content="Completed!",
                tool_calls=[
                    {
                        "id": "call_3",
                        "type": "function",
                        "function": {
                            "name": "todo",
                            "arguments": json.dumps(
                                {
                                    "action": "update",
                                    "task_id": "task_1",
                                    "status": "completed",
                                }
                            ),
                        },
                    }
                ]
                if self.turns == 3
                else None,
            )


def test_coding_agent_tasks_and_background_e2e(tmp_path: Path):
    async def _test():
        release = tmp_path / "release-background"
        worker = tmp_path / "background-worker.py"
        worker.write_text(
            "import sys, time\n"
            "from pathlib import Path\n"
            "release = Path(sys.argv[1])\n"
            "while not release.exists():\n"
            "    time.sleep(0.01)\n"
            "print('All 10 tests passed')\n",
            encoding="utf-8",
        )
        fake_llm = ScriptedCodingLLM(f'"{sys.executable}" "{worker}" "{release}"')
        session = Session(path=tmp_path / "session.jsonl")
        store = TaskStore(tmp_path)

        agent = CodingAgent(
            workspace=tmp_path,
            llm=fake_llm,
            session=session,
            task_store=store,
            memory_dir=False,
            plugin_dirs=[],
            subagent_dirs=[],
        )

        try:
            # The real child cannot complete before this explicit second phase,
            # regardless of coverage overhead or event-loop/process scheduling.
            ans1 = await agent.run("Please run tests in background.")
            assert ans1 == "Waiting for background task..."
            assert not release.exists()
            jobs = list(agent.background_runner.jobs.values())
            assert len(jobs) == 1
            job = jobs[0]
            assert job.status == "running"
            assert job.task is not None
            assert not any("<task_notification" in str(m.content) for m in fake_llm.calls[1])

            release.touch()
            await job.task
            assert job.status == "completed"
            assert job.exit_code == 0
            assert job.result == "All 10 tests passed"

            second_run_start = len(fake_llm.calls)
            ans2 = await agent.run("Check status")
            assert ans2 == "Completed!"
            assert any(
                "<task_notification" in str(m.content) and "All 10 tests passed" in str(m.content)
                for call in fake_llm.calls[second_run_start:]
                for m in call
            )
            assert store.get("task_1").status == "completed"
        finally:
            release.touch(exist_ok=True)
            await agent.background_runner.cancel_all()
            await asyncio.gather(
                *(job.task for job in agent.background_runner.jobs.values() if job.task is not None),
                return_exceptions=True,
            )
            await agent.aclose()

    asyncio.run(_test())
