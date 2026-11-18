"""Pre-registration and paired analysis for long-horizon quality experiments."""

from .analysis import TrialOutcome, analyze
from .orchestrator import (
    AUDITOR_SYSTEM_PROMPT,
    EXECUTOR_SYSTEM_PROMPT,
    MANAGER_SYSTEM_PROMPT,
    MEAOutcome,
    RoleCallbacks,
    RoleMessage,
    RoleRequest,
    run_mea,
)
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
    "AUDITOR_SYSTEM_PROMPT",
    "CONFIRMATORY_TASK_COUNT",
    "DEFAULT_ARMS",
    "EXECUTOR_SYSTEM_PROMPT",
    "MANAGER_SYSTEM_PROMPT",
    "MEAOutcome",
    "PROTOCOL_ID",
    "RoleCallbacks",
    "RoleMessage",
    "RoleRequest",
    "TERMINAL_BENCH_REVISION",
    "ProtocolError",
    "TrialOutcome",
    "analyze",
    "build_slots",
    "freeze_campaign",
    "load_plan",
    "run_mea",
]
