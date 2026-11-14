"""Shared test isolation for developer and hosted-CI environments."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_agent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient user settings and integrations out of the test suite."""
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "agent-home"))
