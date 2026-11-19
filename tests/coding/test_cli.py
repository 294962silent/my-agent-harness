"""Tests for Python CLI entrypoint (my_coding_agent.cli)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from my_coding_agent.cli import ensure_tui_dependencies, find_tui_entry, main


def test_find_tui_entry_in_dev_workspace():
    entry = find_tui_entry()
    assert entry is not None
    assert entry.exists()
    assert entry.name == "my-agent.js"


def test_cli_help_flag_displays_usage(capsys):
    with patch.object(sys, "argv", ["longarc", "--help"]):
        code = main(["--help"])
        # If node exists, it delegates to node --help or prints local help
        # Either way code is 0 or delegated
        out = capsys.readouterr().out
        assert code == 0 or "Usage" in out or "longarc" in out or "LongArc" in out


def test_cli_missing_node_outputs_friendly_guide(capsys):
    with patch("shutil.which", return_value=None):
        code = main(["-m", "gpt-4o"])
        assert code == 1
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "Node.js" in combined or "node" in combined
        assert "https://nodejs.org" in combined or "fnm" in combined or "nvm" in combined


def test_cli_launches_node_with_python_executable():
    # Use an existing real file so exists() returns True naturally
    fake_entry = Path(__file__).resolve()
    with (
        patch("my_coding_agent.cli.find_tui_entry", return_value=fake_entry),
        patch("shutil.which", return_value="/usr/bin/node"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        code = main(["-c", "-m", "deepseek-chat"])
        assert code == 0
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "/usr/bin/node"
        assert str(fake_entry) in call_args[1]
        assert "-c" in call_args
        assert "-m" in call_args
        assert "deepseek-chat" in call_args
        assert "--python-executable" in call_args
        assert sys.executable in call_args


def test_ensure_tui_dependencies_installs_missing_packages(tmp_path: Path):
    tui_root = tmp_path / "my-tui"
    entry = tui_root / "bin" / "my-agent.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    (tui_root / "package.json").write_text("{}", encoding="utf-8")

    completed = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch("my_coding_agent.cli.shutil.which", return_value="C:/npm.cmd"),
        patch("my_coding_agent.cli.subprocess.run", return_value=completed) as mock_run,
    ):
        ensure_tui_dependencies(entry)

    mock_run.assert_called_once_with(
        ["C:/npm.cmd", "install", "--omit=dev", "--ignore-scripts"],
        cwd=tui_root,
        capture_output=True,
        text=True,
        timeout=180,
    )
