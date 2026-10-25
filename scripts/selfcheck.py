"""一键自检：确认这个项目在你机器上「真的能用」。

用法（在仓库根目录）：
    uv run python scripts/selfcheck.py            # 全量（含一次真实模型调用）
    uv run python scripts/selfcheck.py --offline  # 只跑本地检查，不花钱

检查项：
1. 包能否导入、记忆存储能否读写；
2. 事实是否落成 Markdown（可 grep/git），检索走 FTS5 还是 BM25；
3. supersede 是否生效（新值覆盖旧值，旧值进 history）；
4. RPC 服务能否启动并完成一轮真实对话（可选）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROWS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    ROWS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def offline_checks() -> None:
    from my_agent_core.memory import MemoryStore
    from my_agent_core.memory_index import MemoryIndex

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(mem_dir=Path(tmp))
        store.load_from_disk()

        store.remember("wifi_password", "1234", valid_from="2023-01-01")
        first = store.retrieve("what is my wifi password?")
        check("写入事实并可检索", bool(first and "1234" in first))

        store.remember("wifi_password", "9876", valid_from="2023-06-01")
        second = store.retrieve("what is my wifi password?") or ""
        check("新值覆盖旧值（supersede）", "9876" in second and "1234" not in second)

        md = Path(tmp) / "facts" / "user" / "wifi_password.md"
        check("事实落成 Markdown", md.exists(), str(md.relative_to(tmp)) if md.exists() else "")
        if md.exists():
            text = md.read_text(encoding="utf-8")
            check("frontmatter 可读", text.startswith("---") and 'key: "wifi_password"' in text)
            check("变更历史留在文件里", "history:" in text and 'value: "1234"' in text)

    idx = MemoryIndex(
        entries={"memory": ["project uses pytest"]},
        facts=[],
    )
    check("检索后端", True, idx.backend)


async def online_check(timeout: float = 120.0) -> None:
    workspace = tempfile.mkdtemp(prefix="selfcheck-")
    started = time.time()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        "my_coding_agent.rpc_server",
        "-w",
        workspace,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def send(payload: dict) -> None:
        assert proc.stdin
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()

    async def read_one() -> dict | None:
        assert proc.stdout
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return json.loads(line.decode()) if line.strip() else None

    try:
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"workspace": workspace, "mode": "yolo"}})
        await send({"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"text": "只回复两个字：可用"}})

        answer, error = "", None
        while True:
            msg = await read_one()
            if msg is None:
                error = "超时或进程退出"
                break
            if msg.get("error"):
                error = str(msg["error"])
                break
            params = msg.get("params") or {}
            if params.get("type") == "message_update":
                delta = params.get("delta")
                if isinstance(delta, dict):
                    answer += str(delta.get("content") or "")
                elif isinstance(delta, str):
                    answer += delta
            if msg.get("id") == 2 and params.get("type") in (None, "agent_end", "turn_end"):
                break

        elapsed = time.time() - started
        if error:
            check("真实模型对话", False, error)
        else:
            check("真实模型对话", bool(answer.strip()), f"{answer.strip()[:40]!r}, {elapsed:.1f}s")
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="self check")
    parser.add_argument("--offline", action="store_true", help="跳过真实模型调用")
    args = parser.parse_args()

    print(f"Python: {sys.version.split()[0]}")
    print(f"仓库:   {Path(__file__).resolve().parents[1]}\n")

    offline_checks()
    if not args.offline:
        asyncio.run(online_check())

    failed = [r for r in ROWS if not r[1]]
    print()
    if failed:
        print(f"结果：{len(ROWS) - len(failed)}/{len(ROWS)} 通过，{len(failed)} 项失败")
        return 1
    print(f"结果：{len(ROWS)}/{len(ROWS)} 全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
