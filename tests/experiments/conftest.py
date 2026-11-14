"""Test support for repository-relative experiment packages."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Compat3-7 validate immutable campaign evidence that is intentionally kept under
# the developer's local Git metadata. Hosted CI checkouts do not contain that state;
# ignore these optional suites before pytest imports modules that read it.
_LOCAL_EVIDENCE_ROOT = Path(
    os.environ.get(
        "MY_AGENT_RESEARCH_EVIDENCE_ROOT",
        "C:/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/effect-validation-20261114",
    )
).resolve()
_LOCAL_COMPAT2_CAMPAIGN = (
    _LOCAL_EVIDENCE_ROOT / "rsi-study-v4-compat2" / "c-yacht-train" / "campaign.json"
)

if not _LOCAL_COMPAT2_CAMPAIGN.is_file():
    collect_ignore = [
        "test_rsi_compat3.py",
        "test_rsi_compat4.py",
        "test_rsi_compat5.py",
        "test_rsi_compat6.py",
        "test_rsi_compat7.py",
    ]
