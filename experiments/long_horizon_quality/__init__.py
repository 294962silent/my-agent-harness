"""Pre-registration and paired analysis for long-horizon quality experiments."""

from .analysis import TrialOutcome, analyze
from .protocol import (
    ARM_SPECS,
    CONFIRMATORY_TASK_COUNT,
    DEFAULT_ARMS,
    PROTOCOL_ID,
    TERMINAL_BENCH_REVISION,
    ProtocolError,
    build_slots,
    freeze_campaign,
    load_plan,
)

__all__ = [
    "ARM_SPECS",
    "CONFIRMATORY_TASK_COUNT",
    "DEFAULT_ARMS",
    "PROTOCOL_ID",
    "TERMINAL_BENCH_REVISION",
    "ProtocolError",
    "TrialOutcome",
    "analyze",
    "build_slots",
    "freeze_campaign",
    "load_plan",
]
