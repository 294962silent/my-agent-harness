"""把真实任务落成可运行的沙盒工作区。

流程：
1. `git init` 一个临时仓库，`git fetch` 目标 commit（浅拉）；
2. checkout 出 base 版本（修改前）；
3. 把 PR 修改的**测试文件**替换成 head 版本（这样测试反映"应该达到的行为"）；
4. 在 Docker 里跑：安装依赖 → 执行测试 → 期望**失败**（因为源码还是旧的）。

这一步是"验证任务可用性"。只有"旧代码 + 新测试 = 失败"的任务才是有效任务。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

RESULTS = Path(__file__).resolve().parent / "results" / "real"
DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def run(cmd: list[str], cwd: Path, timeout: int = 300, env: dict | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})}, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def fetch_checkout(repo: str, sha: str, dest: Path) -> bool:
    """浅拉取指定 commit 到 dest。"""
    dest.mkdir(parents=True, exist_ok=True)
    steps = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", f"https://github.com/{repo}.git"],
        ["git", "fetch", "--depth", "1", "-q", "origin", sha],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    for step in steps:
        code, out = run(step, dest, timeout=300)
        if code != 0:
            print(f"    git 失败 {step[1]}: {out[:200]}")
            return False
    return True


def fetch_file(repo: str, sha: str, path: str) -> str | None:
    """取某个 commit 下的单个文件内容（用 raw.githubusercontent）。"""
    import urllib.request

    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "harness"}), timeout=40) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def prepare_workspace(task: dict, workdir: Path) -> bool:
    """构建 base + head 测试的工作区。"""
    repo = task["repo"]
    if not fetch_checkout(repo, task["base_sha"], workdir):
        return False
    # 用 head 版本的测试覆盖
    for test_path in task["test_files"]:
        content = fetch_file(repo, task["head_sha"], test_path)
        if content is None:
            continue
        target = workdir / test_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return True


ENV_BROKEN_PATTERNS = (
    "ModuleNotFoundError",
    "ImportError",
    "no tests ran",
    "deselected",
    "No module named",
    "error: unrecognized arguments",
)


def looks_like_env_issue(output: str) -> bool:
    """判断失败是因为环境缺依赖，而不是代码缺陷。"""
    return any(pattern in output for pattern in ENV_BROKEN_PATTERNS)


def apply_head_sources(task: dict, workdir: Path) -> bool:
    """把 PR 的源码改动也应用上（模拟"真实修复"），用于验证任务可达。"""
    applied = 0
    for path in task["changed_files"]:
        content = fetch_file(task["repo"], task["head_sha"], path)
        if content is None:
            continue
        target = workdir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        applied += 1
    return applied > 0


def check_in_docker(workdir: Path, test_files: list[str], timeout: int = 600) -> tuple[bool, str]:
    """在容器里装依赖并跑测试，返回 (是否失败, 输出)。我们要的是"失败"。"""
    script = (
        "set -e\n"
        "pip install -q -e . 2>&1 | tail -3 || true\n"
        "pip install -q pytest 2>&1 | tail -2\n"
        + "\n".join(f'echo "=== {t} ==="; python -m pytest -q "{t}" --no-header -p no:cacheprovider 2>&1 | tail -25 || true' for t in test_files[:3])
    )
    proc = subprocess.run(
        [
            DOCKER, "run", "--rm",
            "-v", f"{workdir}:/workspace", "-w", "/workspace",
            "--cpus", "2", "--memory", "3g", "--network", "host",
            "my-agent-rl:py311", "sh", "-lc", script,
        ],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
        env={**os.environ, "HTTPS_PROXY": "", "HTTP_PROXY": ""},
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    failed = "failed" in out or "error" in out.lower() or proc.returncode != 0
    return failed, out


def verify_batch(tasks: list[dict], out_path: Path, workers: int = 3) -> tuple[int, int]:
    """并行验证：每个任务独立工作区，Docker 并发跑。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    lock = __import__("threading").Lock()
    verified = 0

    def one(task: dict) -> tuple[dict, bool, str]:
        with tempfile.TemporaryDirectory(prefix="real-") as tmp:
            workdir = Path(tmp) / "repo"
            if not prepare_workspace(task, workdir):
                return task, False, "拉取失败"
            try:
                failed, out = check_in_docker(workdir, task["test_files"])
            except subprocess.TimeoutExpired:
                return task, False, "超时"
            if not failed:
                return task, False, "旧代码通过（判定无效）"
            if looks_like_env_issue(out):
                return task, False, "环境缺依赖（不可评测）"

            # 关键校验：打上 head 的真实修复，测试必须能通过
            if not apply_head_sources(task, workdir):
                return task, False, "无法获取 head 源码"
            try:
                still_failed, out2 = check_in_docker(workdir, task["test_files"])
            except subprocess.TimeoutExpired:
                return task, False, "修复后超时"
            if still_failed:
                return task, False, "真实修复也无法通过（任务不可达）"

            task["verified"] = True
            task["check_output_tail"] = out[-600:]
            task["head_pass_output"] = out2[-400:]
            return task, True, out.strip().splitlines()[-1][:70] if out.strip() else "ok"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, task): task for task in tasks}
        for index, future in enumerate(as_completed(futures), 1):
            task, ok, note = future.result()
            with lock:
                if ok:
                    verified += 1
                    with out_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(task, ensure_ascii=False) + "\n")
                print(f"[{index}/{len(tasks)}] {task['task_id']} {'✓' if ok else '✗'} {note}", flush=True)
    return verified, len(tasks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(RESULTS / "real_tasks.jsonl"))
    parser.add_argument("--out", default=str(RESULTS / "verified_tasks.jsonl"))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--skip-docker", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    tasks = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    tasks = tasks[: args.limit]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    if args.skip_docker:
        for task in tasks:
            with tempfile.TemporaryDirectory(prefix="real-") as tmp:
                if prepare_workspace(task, Path(tmp) / "repo"):
                    with out_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(task, ensure_ascii=False) + "\n")
        print("跳过 Docker：仅检查拉取")
        return 0

    verified, total = verify_batch(tasks, out_path, workers=args.workers)
    print(f"\n验证通过: {verified}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
