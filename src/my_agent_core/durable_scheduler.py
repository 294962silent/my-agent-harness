"""SQLite-backed multi-worker queue, leases, quotas, and recovery.

The local ``SandboxScheduler`` remains the fast single-process engine. This
module adds the control-plane state needed when several worker processes share
one finite host: durable jobs, idempotency, heartbeats, TTL leases, fencing
tokens, global resources, retry/recovery, and minute-level RPM/TPM quotas.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Generic, Iterator, Literal, TypeVar
from uuid import uuid4

from my_agent_core.sandbox_scheduler import (
    ExecutionResourcePool,
    SandboxJobResources,
    SandboxJobTimeout,
)


T = TypeVar("T")
DurableJobState = Literal[
    "queued",
    "leased",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
]


def _positive_int(name: str, value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _finite_positive(name: str, value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _identity(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class DurableSchedulerLimits:
    max_active_jobs: int = 8
    max_pending_jobs: int = 256
    max_active_per_tenant: int = 2
    max_pending_per_tenant: int = 64
    max_sandboxes: int = 8
    max_memory_mb: int = 8_192
    max_cpu_slots: int = 8
    max_process_slots: int = 8
    max_thread_slots: int = 32
    global_rpm: int = 600
    global_tpm: int = 1_000_000
    tenant_rpm: int = 120
    tenant_tpm: int = 300_000
    lease_ttl_seconds: float = 30.0
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in (
            "max_active_jobs",
            "max_pending_jobs",
            "max_active_per_tenant",
            "max_pending_per_tenant",
            "max_sandboxes",
            "max_memory_mb",
            "max_cpu_slots",
            "max_process_slots",
            "max_thread_slots",
            "global_rpm",
            "global_tpm",
            "tenant_rpm",
            "tenant_tpm",
        ):
            _positive_int(name, getattr(self, name))
        _finite_positive("lease_ttl_seconds", self.lease_ttl_seconds)
        _finite_positive("retry_base_seconds", self.retry_base_seconds)
        _finite_positive("retry_max_seconds", self.retry_max_seconds)
        if self.max_active_jobs > self.max_sandboxes:
            raise ValueError("max_active_jobs cannot exceed max_sandboxes")
        if self.max_active_per_tenant > self.max_active_jobs:
            raise ValueError(
                "max_active_per_tenant cannot exceed max_active_jobs"
            )
        if self.max_pending_per_tenant > self.max_pending_jobs:
            raise ValueError(
                "max_pending_per_tenant cannot exceed max_pending_jobs"
            )
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError(
                "retry_max_seconds must be >= retry_base_seconds"
            )


@dataclass(frozen=True, slots=True)
class DurableJobRequest:
    tenant_id: str
    session_id: str
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    resources: SandboxJobResources = field(default_factory=SandboxJobResources)
    timeout_seconds: float | None = None
    max_attempts: int = 3
    priority: int = 0

    def __post_init__(self) -> None:
        _identity("tenant_id", self.tenant_id)
        _identity("session_id", self.session_id)
        _identity("idempotency_key", self.idempotency_key)
        if type(self.payload) is not dict:
            raise TypeError("payload must be a dict")
        if not isinstance(self.resources, SandboxJobResources):
            raise TypeError("resources must be SandboxJobResources")
        if type(self.max_attempts) is not int or self.max_attempts <= 0:
            raise ValueError("max_attempts must be a positive integer")
        if type(self.priority) is not int:
            raise ValueError("priority must be an integer")
        if self.timeout_seconds is not None:
            _finite_positive("timeout_seconds", self.timeout_seconds)
        try:
            json.dumps(self.payload, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be finite JSON data") from exc


@dataclass(frozen=True, slots=True)
class DurableJobRecord:
    job_id: str
    idempotency_key: str
    tenant_id: str
    session_id: str
    state: DurableJobState
    payload: dict[str, Any]
    resources: SandboxJobResources
    priority: int
    attempt: int
    max_attempts: int
    timeout_seconds: float | None
    lease_owner: str | None
    fencing_token: int | None
    lease_expires_at: float | None
    cancel_requested: bool
    last_error: str | None
    result: Any | None
    created_at: float
    available_at: float
    finished_at: float | None


@dataclass(frozen=True, slots=True)
class DurableJobClaim:
    job_id: str
    worker_id: str
    tenant_id: str
    session_id: str
    payload: dict[str, Any]
    resources: SandboxJobResources
    timeout_seconds: float | None
    attempt: int
    max_attempts: int
    fencing_token: int
    lease_expires_at: float


@dataclass(frozen=True, slots=True)
class DurableHeartbeat:
    state: Literal["renewed", "cancel_requested", "lost"]
    lease_expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class DurableRateDecision:
    allowed: bool
    retry_after_seconds: float
    global_requests: int
    global_tokens: int
    tenant_requests: int
    tenant_tokens: int


@dataclass(frozen=True, slots=True)
class DurableSchedulerStats:
    queued: int
    active: int
    succeeded: int
    failed: int
    timed_out: int
    cancelled: int
    used_sandboxes: int
    used_memory_mb: int
    active_by_tenant: dict[str, int]


@dataclass(frozen=True, slots=True)
class DurableRunResult:
    claim: DurableJobClaim
    state: DurableJobState
    result: Any | None = None
    error: str | None = None
    lease_lost: bool = False


class DurableAdmissionRejected(RuntimeError):
    def __init__(self, reason: str, *, tenant_id: str) -> None:
        self.reason = reason
        self.tenant_id = tenant_id
        super().__init__(
            f"durable admission rejected ({reason}): tenant={tenant_id!r}"
        )


class DurableIdempotencyConflict(RuntimeError):
    pass


class DurableConfigurationMismatch(RuntimeError):
    pass


class DurableRateLimited(RuntimeError):
    def __init__(self, decision: DurableRateDecision) -> None:
        self.decision = decision
        super().__init__(
            "rate limit exceeded; retry after "
            f"{decision.retry_after_seconds:.3f}s"
        )


class DurableRetryableError(RuntimeError):
    """Runner error that should return the job to the durable queue."""

    def __init__(self, message: str, *, retry_after_seconds: float = 0.1) -> None:
        self.retry_after_seconds = _finite_positive(
            "retry_after_seconds",
            retry_after_seconds,
        )
        super().__init__(message)


class DurableSandboxExecution:
    """Execution handle owned by a claimed durable job."""

    def __init__(
        self,
        scheduler: DurableSandboxScheduler,
        claim: DurableJobClaim,
        resource_pool: ExecutionResourcePool,
        cancelled: asyncio.Event,
    ) -> None:
        self._scheduler = scheduler
        self.claim = claim
        self.job_id = claim.job_id
        self.tenant_id = claim.tenant_id
        self.session_id = claim.session_id
        self.fencing_token = claim.fencing_token
        self.memory_limit_mb = claim.resources.memory_mb
        self.resources = claim.resources
        self._resource_pool = resource_pool
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def assert_active(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError("durable sandbox execution was cancelled")

    @asynccontextmanager
    async def lease(
        self,
        *,
        cpu_slots: int = 0,
        process_slots: int = 0,
        thread_slots: int = 0,
    ) -> AsyncIterator[DurableSandboxExecution]:
        self.assert_active()
        await self._resource_pool.acquire(
            cpu_slots=cpu_slots,
            process_slots=process_slots,
            thread_slots=thread_slots,
        )
        try:
            self.assert_active()
            yield self
        finally:
            await self._resource_pool.release(
                cpu_slots=cpu_slots,
                process_slots=process_slots,
                thread_slots=thread_slots,
            )

    def reserve_model_request(
        self,
        estimated_tokens: int,
    ) -> DurableRateDecision:
        decision = self._scheduler.reserve_model_request(
            self.tenant_id,
            estimated_tokens,
        )
        if not decision.allowed:
            raise DurableRateLimited(decision)
        return decision


class DurableSandboxScheduler:
    """Transactional control plane shared by worker processes."""

    _SCHEMA_VERSION = 1

    def __init__(
        self,
        db_path: str | Path,
        limits: DurableSchedulerLimits | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        path = Path(db_path)
        if str(path) == ":memory:":
            raise ValueError("durable scheduler requires a filesystem database")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = path
        self.limits = limits or DurableSchedulerLimits()
        self._clock = clock
        self._initialize()

    def submit(self, request: DurableJobRequest) -> DurableJobRecord:
        now = self._clock()
        request_hash = self._request_hash(request)
        job_id = uuid4().hex
        with self._transaction() as conn:
            self._expire_leases(conn, now)
            existing = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise DurableIdempotencyConflict(
                        "idempotency_key reused with a different request"
                    )
                return self._row_to_job(existing)

            pending = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE state = 'queued'"
            ).fetchone()[0]
            tenant_pending = conn.execute(
                "SELECT COUNT(*) FROM jobs "
                "WHERE state = 'queued' AND tenant_id = ?",
                (request.tenant_id,),
            ).fetchone()[0]
            if pending >= self.limits.max_pending_jobs:
                raise DurableAdmissionRejected(
                    "global_queue_full",
                    tenant_id=request.tenant_id,
                )
            if tenant_pending >= self.limits.max_pending_per_tenant:
                raise DurableAdmissionRejected(
                    "tenant_queue_full",
                    tenant_id=request.tenant_id,
                )
            self._validate_resources(request)
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, idempotency_key, request_hash, tenant_id,
                    session_id, payload_json, state, priority, created_at,
                    available_at, attempt, max_attempts, sandbox_slots,
                    memory_mb, timeout_seconds, cancel_requested
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, 0, ?, ?, ?, ?, 0)
                """,
                (
                    job_id,
                    request.idempotency_key,
                    request_hash,
                    request.tenant_id,
                    request.session_id,
                    json.dumps(
                        request.payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    request.priority,
                    now,
                    now,
                    request.max_attempts,
                    request.resources.sandbox_slots,
                    request.resources.memory_mb,
                    request.timeout_seconds,
                ),
            )
        job = self.get_job(job_id)
        assert job is not None
        return job

    def claim(self, worker_id: str) -> DurableJobClaim | None:
        worker_id = _identity("worker_id", worker_id)
        now = self._clock()
        with self._transaction() as conn:
            self._expire_leases(conn, now)
            self._register_worker(conn, worker_id, now)
            usage = conn.execute(
                """
                SELECT COUNT(*) AS active,
                       COALESCE(SUM(sandbox_slots), 0) AS sandboxes,
                       COALESCE(SUM(memory_mb), 0) AS memory_mb
                FROM jobs
                WHERE state IN ('leased', 'running')
                """
            ).fetchone()
            active = int(usage["active"])
            used_sandboxes = int(usage["sandboxes"])
            used_memory = int(usage["memory_mb"])
            if active >= self.limits.max_active_jobs:
                return None

            tenant_rows = conn.execute(
                """
                SELECT tenant_id, COUNT(*) AS active
                FROM jobs
                WHERE state IN ('leased', 'running')
                GROUP BY tenant_id
                """
            ).fetchall()
            active_by_tenant = {
                row["tenant_id"]: int(row["active"]) for row in tenant_rows
            }
            candidates = conn.execute(
                """
                SELECT j.*
                FROM jobs AS j
                LEFT JOIN tenant_state AS ts ON ts.tenant_id = j.tenant_id
                WHERE j.state = 'queued'
                  AND j.available_at <= ?
                  AND j.cancel_requested = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs AS active
                      WHERE active.session_id = j.session_id
                        AND active.state IN ('leased', 'running')
                  )
                ORDER BY COALESCE(ts.last_served_at, 0) ASC,
                         j.priority DESC,
                         j.created_at ASC,
                         j.rowid ASC
                LIMIT 1000
                """,
                (now,),
            ).fetchall()

            selected = None
            for candidate in candidates:
                tenant_id = candidate["tenant_id"]
                if active_by_tenant.get(tenant_id, 0) >= (
                    self.limits.max_active_per_tenant
                ):
                    continue
                if (
                    used_sandboxes + candidate["sandbox_slots"]
                    > self.limits.max_sandboxes
                ):
                    continue
                if (
                    used_memory + candidate["memory_mb"]
                    > self.limits.max_memory_mb
                ):
                    continue
                selected = candidate
                break
            if selected is None:
                return None

            fencing_token = self._next_fencing_token(conn)
            lease_expires = now + self.limits.lease_ttl_seconds
            attempt = int(selected["attempt"]) + 1
            updated = conn.execute(
                """
                UPDATE jobs
                SET state = 'leased',
                    attempt = ?,
                    lease_owner = ?,
                    fencing_token = ?,
                    lease_expires_at = ?
                WHERE job_id = ? AND state = 'queued'
                """,
                (
                    attempt,
                    worker_id,
                    fencing_token,
                    lease_expires,
                    selected["job_id"],
                ),
            )
            if updated.rowcount != 1:
                return None
            conn.execute(
                """
                INSERT INTO tenant_state (tenant_id, last_served_at)
                VALUES (?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    last_served_at = excluded.last_served_at
                """,
                (selected["tenant_id"], now),
            )
            return DurableJobClaim(
                job_id=selected["job_id"],
                worker_id=worker_id,
                tenant_id=selected["tenant_id"],
                session_id=selected["session_id"],
                payload=json.loads(selected["payload_json"]),
                resources=SandboxJobResources(
                    sandbox_slots=int(selected["sandbox_slots"]),
                    memory_mb=int(selected["memory_mb"]),
                ),
                timeout_seconds=selected["timeout_seconds"],
                attempt=attempt,
                max_attempts=int(selected["max_attempts"]),
                fencing_token=fencing_token,
                lease_expires_at=lease_expires,
            )

    def mark_running(self, claim: DurableJobClaim) -> bool:
        with self._transaction() as conn:
            result = conn.execute(
                """
                UPDATE jobs SET state = 'running'
                WHERE job_id = ? AND state = 'leased'
                  AND lease_owner = ? AND fencing_token = ?
                """,
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            )
            return result.rowcount == 1

    def heartbeat(self, claim: DurableJobClaim) -> DurableHeartbeat:
        now = self._clock()
        with self._transaction() as conn:
            self._expire_leases(conn, now)
            row = conn.execute(
                """
                SELECT state, cancel_requested
                FROM jobs
                WHERE job_id = ? AND lease_owner = ? AND fencing_token = ?
                """,
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            ).fetchone()
            if row is None or row["state"] not in ("leased", "running"):
                return DurableHeartbeat("lost")
            if row["cancel_requested"]:
                return DurableHeartbeat("cancel_requested")
            expires = now + self.limits.lease_ttl_seconds
            conn.execute(
                """
                UPDATE jobs SET lease_expires_at = ?
                WHERE job_id = ? AND lease_owner = ? AND fencing_token = ?
                """,
                (
                    expires,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            )
            self._register_worker(conn, claim.worker_id, now)
            return DurableHeartbeat("renewed", expires)

    def finish(
        self,
        claim: DurableJobClaim,
        state: Literal["succeeded", "failed", "timed_out", "cancelled"],
        *,
        result: Any | None = None,
        error: str | None = None,
    ) -> bool:
        try:
            result_json = (
                None
                if result is None
                else json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("result must be JSON serializable") from exc
        now = self._clock()
        with self._transaction() as conn:
            self._expire_leases(conn, now)
            updated = conn.execute(
                """
                UPDATE jobs
                SET state = ?, result_json = ?, last_error = ?,
                    lease_owner = NULL, lease_expires_at = NULL,
                    finished_at = ?
                WHERE job_id = ? AND state IN ('leased', 'running')
                  AND lease_owner = ? AND fencing_token = ?
                """,
                (
                    state,
                    result_json,
                    error,
                    now,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            )
            self._register_worker(conn, claim.worker_id, now)
            return updated.rowcount == 1

    def retry(
        self,
        claim: DurableJobClaim,
        *,
        error: str,
        retry_after_seconds: float,
    ) -> bool:
        delay = _finite_positive(
            "retry_after_seconds",
            retry_after_seconds,
        )
        now = self._clock()
        with self._transaction() as conn:
            self._expire_leases(conn, now)
            row = conn.execute(
                """
                SELECT state, attempt, max_attempts, cancel_requested
                FROM jobs
                WHERE job_id = ? AND lease_owner = ? AND fencing_token = ?
                """,
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            ).fetchone()
            if row is None or row["state"] not in ("leased", "running"):
                return False
            if row["cancel_requested"]:
                state = "cancelled"
            elif int(row["attempt"]) < int(row["max_attempts"]):
                state = "queued"
            else:
                state = "failed"
            conn.execute(
                """
                UPDATE jobs
                SET state = ?, available_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = ?,
                    finished_at = CASE WHEN ? IN
                        ('failed', 'cancelled') THEN ? ELSE NULL END
                WHERE job_id = ? AND lease_owner = ? AND fencing_token = ?
                """,
                (
                    state,
                    now + delay,
                    error,
                    state,
                    now,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            )
            self._register_worker(conn, claim.worker_id, now)
            return True

    def request_cancel(self, job_id: str) -> bool:
        now = self._clock()
        with self._transaction() as conn:
            self._expire_leases(conn, now)
            row = conn.execute(
                "SELECT state FROM jobs WHERE job_id = ?",
                (_identity("job_id", job_id),),
            ).fetchone()
            if row is None or row["state"] in (
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
            ):
                return False
            if row["state"] == "queued":
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'cancelled', cancel_requested = 1,
                        finished_at = ?, last_error = 'cancelled before claim'
                    WHERE job_id = ?
                    """,
                    (now, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET cancel_requested = 1 WHERE job_id = ?",
                    (job_id,),
                )
            return True

    def recover_expired(self) -> int:
        with self._transaction() as conn:
            return self._expire_leases(conn, self._clock())

    def reserve_model_request(
        self,
        tenant_id: str,
        estimated_tokens: int,
    ) -> DurableRateDecision:
        tenant_id = _identity("tenant_id", tenant_id)
        if type(estimated_tokens) is not int or estimated_tokens < 0:
            raise ValueError("estimated_tokens must be a non-negative integer")
        now = self._clock()
        minute = int(now // 60)
        retry_after = max(0.001, 60.0 - (now % 60.0))
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM rate_buckets WHERE minute_start < ?",
                (minute - 2,),
            )
            global_scope = "global"
            tenant_scope = f"tenant:{tenant_id}"
            global_row = self._rate_row(conn, global_scope, minute)
            tenant_row = self._rate_row(conn, tenant_scope, minute)
            if (
                global_row["requests"] + 1 > self.limits.global_rpm
                or global_row["tokens"] + estimated_tokens
                > self.limits.global_tpm
                or tenant_row["requests"] + 1 > self.limits.tenant_rpm
                or tenant_row["tokens"] + estimated_tokens
                > self.limits.tenant_tpm
            ):
                return DurableRateDecision(
                    allowed=False,
                    retry_after_seconds=retry_after,
                    global_requests=global_row["requests"],
                    global_tokens=global_row["tokens"],
                    tenant_requests=tenant_row["requests"],
                    tenant_tokens=tenant_row["tokens"],
                )
            self._increment_rate(conn, global_scope, minute, estimated_tokens)
            self._increment_rate(conn, tenant_scope, minute, estimated_tokens)
            return DurableRateDecision(
                allowed=True,
                retry_after_seconds=0.0,
                global_requests=global_row["requests"] + 1,
                global_tokens=global_row["tokens"] + estimated_tokens,
                tenant_requests=tenant_row["requests"] + 1,
                tenant_tokens=tenant_row["tokens"] + estimated_tokens,
            )

    def get_job(self, job_id: str) -> DurableJobRecord | None:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (_identity("job_id", job_id),),
            ).fetchone()
            return None if row is None else self._row_to_job(row)

    def get_jobs(
        self,
        job_ids: Sequence[str],
    ) -> list[DurableJobRecord | None]:
        """Read many job snapshots through one SQLite connection."""
        requested = [_identity("job_id", job_id) for job_id in job_ids]
        unique = list(dict.fromkeys(requested))
        if not unique:
            return []
        placeholders = ",".join("?" for _ in unique)
        with self._read_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE job_id IN ({placeholders})",
                unique,
            ).fetchall()
        by_id = {row["job_id"]: self._row_to_job(row) for row in rows}
        return [by_id.get(job_id) for job_id in requested]

    def job_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> DurableJobRecord | None:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (_identity("idempotency_key", idempotency_key),),
            ).fetchone()
            return None if row is None else self._row_to_job(row)

    def stats(self) -> DurableSchedulerStats:
        with self._read_connection() as conn:
            states = {
                row["state"]: int(row["count"])
                for row in conn.execute(
                    "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
                )
            }
            usage = conn.execute(
                """
                SELECT COALESCE(SUM(sandbox_slots), 0) AS sandboxes,
                       COALESCE(SUM(memory_mb), 0) AS memory_mb
                FROM jobs WHERE state IN ('leased', 'running')
                """
            ).fetchone()
            tenants = {
                row["tenant_id"]: int(row["count"])
                for row in conn.execute(
                    """
                    SELECT tenant_id, COUNT(*) AS count
                    FROM jobs WHERE state IN ('leased', 'running')
                    GROUP BY tenant_id
                    """
                )
            }
        return DurableSchedulerStats(
            queued=states.get("queued", 0),
            active=states.get("leased", 0) + states.get("running", 0),
            succeeded=states.get("succeeded", 0),
            failed=states.get("failed", 0),
            timed_out=states.get("timed_out", 0),
            cancelled=states.get("cancelled", 0),
            used_sandboxes=int(usage["sandboxes"]),
            used_memory_mb=int(usage["memory_mb"]),
            active_by_tenant=tenants,
        )

    def list_workers(self) -> list[dict[str, Any]]:
        with self._read_connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM workers ORDER BY worker_id"
                )
            ]

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    available_at REAL NOT NULL,
                    attempt INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    sandbox_slots INTEGER NOT NULL,
                    memory_mb INTEGER NOT NULL,
                    timeout_seconds REAL,
                    lease_owner TEXT,
                    fencing_token INTEGER,
                    lease_expires_at REAL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    result_json TEXT,
                    finished_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_session
                ON jobs(session_id)
                WHERE state IN ('leased', 'running');
                CREATE INDEX IF NOT EXISTS jobs_queue
                ON jobs(state, available_at, priority, created_at);
                CREATE TABLE IF NOT EXISTS tenant_state (
                    tenant_id TEXT PRIMARY KEY,
                    last_served_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rate_buckets (
                    scope TEXT NOT NULL,
                    minute_start INTEGER NOT NULL,
                    requests INTEGER NOT NULL,
                    tokens INTEGER NOT NULL,
                    PRIMARY KEY(scope, minute_start)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            stored = conn.execute(
                "SELECT value FROM meta WHERE key = 'config'"
            ).fetchone()
            config = self._configuration_json()
            if stored is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('config', ?)",
                    (config,),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) "
                    "VALUES ('next_fencing_token', '0')"
                )
            elif stored["value"] != config:
                conn.execute("ROLLBACK")
                raise DurableConfigurationMismatch(
                    "SQLite file was initialized with different scheduler limits"
                )
            conn.execute("COMMIT")

    def _configuration_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self._SCHEMA_VERSION,
                "max_active_jobs": self.limits.max_active_jobs,
                "max_pending_jobs": self.limits.max_pending_jobs,
                "max_active_per_tenant": self.limits.max_active_per_tenant,
                "max_pending_per_tenant": self.limits.max_pending_per_tenant,
                "max_sandboxes": self.limits.max_sandboxes,
                "max_memory_mb": self.limits.max_memory_mb,
                "global_rpm": self.limits.global_rpm,
                "global_tpm": self.limits.global_tpm,
                "tenant_rpm": self.limits.tenant_rpm,
                "tenant_tpm": self.limits.tenant_tpm,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            yield conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _expire_leases(self, conn: sqlite3.Connection, now: float) -> int:
        expired = conn.execute(
            """
            SELECT * FROM jobs
            WHERE state IN ('leased', 'running')
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            """,
            (now,),
        ).fetchall()
        for row in expired:
            if row["cancel_requested"]:
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'cancelled', lease_owner = NULL,
                        lease_expires_at = NULL, finished_at = ?,
                        last_error = 'cancelled after lease expiry'
                    WHERE job_id = ?
                    """,
                    (now, row["job_id"]),
                )
                continue
            if int(row["attempt"]) < int(row["max_attempts"]):
                delay = min(
                    self.limits.retry_max_seconds,
                    self.limits.retry_base_seconds
                    * (2 ** max(0, int(row["attempt"]) - 1)),
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'queued', available_at = ?,
                        lease_owner = NULL, lease_expires_at = NULL,
                        last_error = 'lease_expired'
                    WHERE job_id = ?
                    """,
                    (now + delay, row["job_id"]),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'failed', lease_owner = NULL,
                        lease_expires_at = NULL, finished_at = ?,
                        last_error = 'max_attempts_exhausted_after_lease_expiry'
                    WHERE job_id = ?
                    """,
                    (now, row["job_id"]),
                )
        return len(expired)

    def _next_fencing_token(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'next_fencing_token'"
        ).fetchone()
        current = int(row["value"])
        token = current + 1
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'next_fencing_token'",
            (str(token),),
        )
        return token

    def _register_worker(
        self,
        conn: sqlite3.Connection,
        worker_id: str,
        now: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO workers(worker_id, started_at, last_seen, status)
            VALUES (?, ?, ?, 'active')
            ON CONFLICT(worker_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                status = 'active'
            """,
            (worker_id, now, now),
        )

    @staticmethod
    def _rate_row(
        conn: sqlite3.Connection,
        scope: str,
        minute: int,
    ) -> dict[str, int]:
        row = conn.execute(
            """
            SELECT requests, tokens FROM rate_buckets
            WHERE scope = ? AND minute_start = ?
            """,
            (scope, minute),
        ).fetchone()
        if row is None:
            return {"requests": 0, "tokens": 0}
        return {
            "requests": int(row["requests"]),
            "tokens": int(row["tokens"]),
        }

    @staticmethod
    def _increment_rate(
        conn: sqlite3.Connection,
        scope: str,
        minute: int,
        tokens: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO rate_buckets(scope, minute_start, requests, tokens)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(scope, minute_start) DO UPDATE SET
                requests = requests + 1,
                tokens = tokens + excluded.tokens
            """,
            (scope, minute, tokens),
        )

    def _validate_resources(self, request: DurableJobRequest) -> None:
        if request.resources.sandbox_slots > self.limits.max_sandboxes:
            raise DurableAdmissionRejected(
                "sandbox_capacity_exceeded",
                tenant_id=request.tenant_id,
            )
        if request.resources.memory_mb > self.limits.max_memory_mb:
            raise DurableAdmissionRejected(
                "memory_capacity_exceeded",
                tenant_id=request.tenant_id,
            )

    @staticmethod
    def _request_hash(request: DurableJobRequest) -> str:
        payload = json.dumps(
            {
                "tenant_id": request.tenant_id,
                "session_id": request.session_id,
                "payload": request.payload,
                "sandbox_slots": request.resources.sandbox_slots,
                "memory_mb": request.resources.memory_mb,
                "timeout_seconds": request.timeout_seconds,
                "max_attempts": request.max_attempts,
                "priority": request.priority,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> DurableJobRecord:
        return DurableJobRecord(
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            tenant_id=row["tenant_id"],
            session_id=row["session_id"],
            state=row["state"],
            payload=json.loads(row["payload_json"]),
            resources=SandboxJobResources(
                sandbox_slots=int(row["sandbox_slots"]),
                memory_mb=int(row["memory_mb"]),
            ),
            priority=int(row["priority"]),
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            timeout_seconds=row["timeout_seconds"],
            lease_owner=row["lease_owner"],
            fencing_token=row["fencing_token"],
            lease_expires_at=row["lease_expires_at"],
            cancel_requested=bool(row["cancel_requested"]),
            last_error=row["last_error"],
            result=(
                None
                if row["result_json"] is None
                else json.loads(row["result_json"])
            ),
            created_at=float(row["created_at"]),
            available_at=float(row["available_at"]),
            finished_at=row["finished_at"],
        )


Runner = Callable[[DurableSandboxExecution], Awaitable[T]]


class DurableSandboxWorker(Generic[T]):
    """Pull jobs from SQLite, heartbeat the lease, and reap on cancellation."""

    def __init__(
        self,
        scheduler: DurableSandboxScheduler,
        *,
        worker_id: str,
        poll_seconds: float = 0.1,
    ) -> None:
        self.scheduler = scheduler
        self.worker_id = _identity("worker_id", worker_id)
        self.poll_seconds = _finite_positive("poll_seconds", poll_seconds)
        self._resource_pool = ExecutionResourcePool(
            cpu_limit=scheduler.limits.max_cpu_slots,
            process_limit=scheduler.limits.max_process_slots,
            thread_limit=scheduler.limits.max_thread_slots,
        )

    async def run_once(
        self,
        runner: Runner[T],
    ) -> DurableRunResult | None:
        claim = await asyncio.to_thread(self.scheduler.claim, self.worker_id)
        if claim is None:
            return None
        await asyncio.to_thread(self.scheduler.mark_running, claim)
        cancelled = asyncio.Event()
        execution = DurableSandboxExecution(
            self.scheduler,
            claim,
            self._resource_pool,
            cancelled,
        )
        runner_task = asyncio.create_task(
            runner(execution),
            name=f"durable-sandbox-job:{claim.job_id[:12]}",
        )
        monitor_task = asyncio.create_task(
            self._monitor_lease(claim, cancelled),
            name=f"durable-lease:{claim.job_id[:12]}",
        )
        try:
            done, _ = await asyncio.wait(
                {runner_task, monitor_task},
                timeout=claim.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await self._cancel_runner(runner_task)
                await asyncio.to_thread(
                    self.scheduler.finish,
                    claim,
                    "timed_out",
                    error="job timeout",
                )
                return DurableRunResult(
                    claim=claim,
                    state="timed_out",
                    error="job timeout",
                )

            if monitor_task in done:
                reason = monitor_task.result()
                await self._cancel_runner(runner_task)
                if reason == "lease_lost":
                    return DurableRunResult(
                        claim=claim,
                        state="failed",
                        error="lease expired",
                        lease_lost=True,
                    )
                await asyncio.to_thread(
                    self.scheduler.finish,
                    claim,
                    "cancelled",
                    error="cancel requested",
                )
                return DurableRunResult(
                    claim=claim,
                    state="cancelled",
                    error="cancel requested",
                )

            try:
                result = runner_task.result()
            except asyncio.CancelledError:
                await asyncio.to_thread(
                    self.scheduler.finish,
                    claim,
                    "cancelled",
                    error="runner cancelled",
                )
                return DurableRunResult(
                    claim=claim,
                    state="cancelled",
                    error="runner cancelled",
                )
            except SandboxJobTimeout as exc:
                await asyncio.to_thread(
                    self.scheduler.finish,
                    claim,
                    "timed_out",
                    error=str(exc),
                )
                return DurableRunResult(
                    claim=claim,
                    state="timed_out",
                    error=str(exc),
                )
            except DurableRateLimited as exc:
                retried = await asyncio.to_thread(
                    self.scheduler.retry,
                    claim,
                    error=str(exc),
                    retry_after_seconds=exc.decision.retry_after_seconds,
                )
                return DurableRunResult(
                    claim=claim,
                    state="queued" if retried else "failed",
                    error=str(exc),
                    lease_lost=not retried,
                )
            except DurableRetryableError as exc:
                retried = await asyncio.to_thread(
                    self.scheduler.retry,
                    claim,
                    error=str(exc),
                    retry_after_seconds=exc.retry_after_seconds,
                )
                return DurableRunResult(
                    claim=claim,
                    state="queued" if retried else "failed",
                    error=str(exc),
                    lease_lost=not retried,
                )
            except BaseException as exc:
                await asyncio.to_thread(
                    self.scheduler.finish,
                    claim,
                    "failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                return DurableRunResult(
                    claim=claim,
                    state="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

            await asyncio.to_thread(
                self.scheduler.finish,
                claim,
                "succeeded",
                result=result,
            )
            return DurableRunResult(
                claim=claim,
                state="succeeded",
                result=result,
            )
        except asyncio.CancelledError:
            cancelled.set()
            await self._cancel_runner(runner_task)
            await asyncio.to_thread(
                self.scheduler.finish,
                claim,
                "cancelled",
                error="worker cancelled",
            )
            raise
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)

    async def run_forever(
        self,
        runner: Runner[T],
        *,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            result = await self.run_once(runner)
            if result is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass

    async def _monitor_lease(
        self,
        claim: DurableJobClaim,
        cancelled: asyncio.Event,
    ) -> str:
        interval = max(0.01, self.scheduler.limits.lease_ttl_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            heartbeat = await asyncio.to_thread(
                self.scheduler.heartbeat,
                claim,
            )
            if heartbeat.state == "lost":
                cancelled.set()
                return "lease_lost"
            if heartbeat.state == "cancel_requested":
                cancelled.set()
                return "cancel_requested"

    @staticmethod
    async def _cancel_runner(task: asyncio.Task[T]) -> None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "DurableAdmissionRejected",
    "DurableConfigurationMismatch",
    "DurableHeartbeat",
    "DurableIdempotencyConflict",
    "DurableJobClaim",
    "DurableJobRecord",
    "DurableJobRequest",
    "DurableJobState",
    "DurableRateDecision",
    "DurableRateLimited",
    "DurableRetryableError",
    "DurableRunResult",
    "DurableSandboxExecution",
    "DurableSandboxScheduler",
    "DurableSandboxWorker",
    "DurableSchedulerLimits",
    "DurableSchedulerStats",
]
