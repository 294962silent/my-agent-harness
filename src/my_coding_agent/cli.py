"""LongArc Python CLI entrypoint facade.

This module provides the main() entrypoint when LongArc is launched via
Python (e.g. `uvx longarc`, `uv tool install longarc`, or `python -m my_coding_agent.cli`).
It detects the system Node.js runtime and bridges to the Pi-TUI frontend.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def find_tui_entry() -> Path | None:
    """Find the LongArc TUI entrypoint across development and packaged locations."""
    repo_root = Path(__file__).resolve().parent.parent.parent

    # 1. Development workspace tree: <repo_root>/my-tui/bin/my-agent.js
    dev_path = repo_root / "my-tui" / "bin" / "my-agent.js"
    if dev_path.exists():
        return dev_path

    # 2. Package-bundled data directory: <package_dir>/my-tui/bin/my-agent.js
    pkg_bundled = Path(__file__).resolve().parent / "my-tui" / "bin" / "my-agent.js"
    if pkg_bundled.exists():
        return pkg_bundled

    # 3. Installed package data. Check the LongArc package first, then legacy names.
    for share_name in ("longarc", "my-agent-harness", "my-pi-agent"):
        prefix_path = (
            Path(sys.prefix)
            / "share"
            / share_name
            / "my-tui"
            / "bin"
            / "my-agent.js"
        )
        if prefix_path.exists():
            return prefix_path

    return None


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint bridging to the Pi-TUI frontend."""
    if argv is None:
        argv = sys.argv[1:]

    node_bin = shutil.which("node")
    if not node_bin:
        print(
            "\n[LongArc] 错误: 未检测到 Node.js 运行时环境。\n"
            "LongArc 采用 Pi-TUI 呈现层，需要 Node.js (>= 18.0) 运行交互界面。\n\n"
            "请安装 Node.js 后重试:\n"
            "  - 官方网站下载: https://nodejs.org\n"
            "  - 或使用极速版本管理器安装: fnm (https://github.com/Schniz/fnm) 或 nvm\n",
            file=sys.stderr,
        )
        return 1

    tui_entry = find_tui_entry()
    if not tui_entry or not tui_entry.exists():
        print(
            "\n[LongArc] 错误: 未找到 TUI 启动入口脚本。\n"
            "请确保已正确构建前端: npm run build --prefix my-tui\n",
            file=sys.stderr,
        )
        return 1

    cmd = [node_bin, str(tui_entry), *argv]
    if "--python-executable" not in argv:
        cmd.extend(["--python-executable", sys.executable])

    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[LongArc] 启动异常: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
