"""Shared fail-closed scaffolding for the two LoCoMo follow-up studies."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

from experiments.benchmark_live.budget import BudgetStop
from experiments.memory_validation.multisession_observation import _object, query_cloud
from experiments.memory_validation.multisession_observation_v3 import (
    ExperimentLLMOwnerV3,
    ReadbackAmbiguous,
    ReadbackError,
    ReadbackTimeout,
    checked_usage,
)
from experiments.memory_validation.multisession_protocol import sha
from experiments.memory_validation.runner import token_state
from my_agent_llm.models import Message


READBACK_WINDOW_SECONDS = 180.0
READBACK_INITIAL_DELAY_SECONDS = 1.0
READBACK_MAX_DELAY_SECONDS = 30.0
LANGFUSE_CLOUD_BASE_URL = "https://cloud.langfuse.com"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value, *, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if not replace:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(root):
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): {"bytes": path.stat().st_size, "sha256": file_sha(path)}
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }


def seal(root, data_name, value):
    root = Path(root)
    write_json(root / data_name, value)
    write_json(root / "manifest.json", inventory(root))
    return file_sha(root / data_name)


def verify_seal(root, data_name):
    root = Path(root).resolve()
    manifest = read_json(root / "manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError("frozen inventory differs")
    for relative, expected in manifest.items():
        path = root / relative
        if (
            not path.is_file()
            or path.stat().st_size != expected.get("bytes")
            or file_sha(path) != expected.get("sha256")
        ):
            raise ValueError("frozen inventory differs")
    return read_json(root / data_name)


def install_live_credential():
    """Move the configured endpoint credential into memory without exposing it."""
    if os.environ.get("LIVE_BENCH_API_KEY"):
        return "live_environment"
    from my_agent_llm.auth import AuthManager
    from my_coding_agent.paths import AgentPaths
    from experiments.memory_validation.budget_v3 import LIVE_BASE_URL

    credential = AuthManager(auth_path=AgentPaths().auth_path).get_credential("openai")
    if credential is None or (credential.base_url or "").rstrip("/") != LIVE_BASE_URL.rstrip("/"):
        raise ValueError("configured live-model credential endpoint is unavailable")
    os.environ["LIVE_BENCH_API_KEY"] = credential.resolve_key()
    return "configured_openai_endpoint"


def require_langfuse_credentials():
    if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
        raise ValueError("required Langfuse credentials are unavailable")


def required_owner(raw, campaign_id):
    return ExperimentLLMOwnerV3.from_environment(
        raw,
        campaign_id=campaign_id,
        enabled=True,
        required=True,
        base_url=LANGFUSE_CLOUD_BASE_URL,
    )


def matching_generation_rows(rows, root, *, campaign, project, model, arm, stage, task_id, usage):
    usage = checked_usage(usage)
    trace = hashlib.sha256(root.encode()).hexdigest()[:32]
    matches = []
    for row in rows:
        try:
            meta = _object(row.get("metadata"))
            wanted = {
                "root_id": root,
                "campaign": campaign,
                "arm": arm,
                "stage": stage,
                "task_id": task_id,
                "status": "ok",
            }
            usage_details = row.get("usageDetails") or {}
            if (
                any(meta.get(key) != value for key, value in wanted.items())
                or row.get("projectId") != project
                or row.get("traceId") != trace
                or row.get("sessionId") != campaign
                or str(row.get("type", "")).upper() != "GENERATION"
                or not row.get("endTime")
                or row.get("model") != model
                or usage_details.get("total") != usage["total_tokens"]
                or usage_details.get("output") != usage["completion_tokens"]
                or usage_details.get("input") != usage["total_tokens"] - usage["completion_tokens"]
            ):
                continue
            provider = _object(meta.get("provider_usage")) if "provider_usage" in meta else {
                "prompt_tokens": meta.get("provider_usage.prompt_tokens"),
                "completion_tokens": meta.get("provider_usage.completion_tokens"),
                "total_tokens": meta.get("provider_usage.total_tokens"),
            }
            if any(provider.get(name) != value for name, value in usage.items()):
                continue
            matches.append(
                {
                    "trace_id": trace,
                    "observation_id": row.get("id"),
                    "session_id": campaign,
                    "project_id": project,
                    "root_id": root,
                    "arm": arm,
                    "stage": stage,
                    "task_id": task_id,
                    "model": model,
                    "usage_details": {
                        key: usage_details[key] for key in ("input", "output", "total") if key in usage_details
                    },
                    "metadata_only": True,
                }
            )
        except (ValueError, TypeError):
            continue
    return matches


class FirstTwoReadback:
    """Verify exactly the first two settled calls through bounded metadata polling."""

    def __init__(
        self,
        owner,
        *,
        campaign,
        project,
        model,
        receipt,
        protocol,
        query=query_cloud,
        wait=time.sleep,
        monotonic=time.monotonic,
        window_seconds=READBACK_WINDOW_SECONDS,
        initial_delay_seconds=READBACK_INITIAL_DELAY_SECONDS,
        max_delay_seconds=READBACK_MAX_DELAY_SECONDS,
    ):
        if not 0 < window_seconds <= READBACK_WINDOW_SECONDS:
            raise ValueError("readback window must be at most 180 seconds")
        if not 0 < initial_delay_seconds <= max_delay_seconds:
            raise ValueError("readback backoff differs")
        self.owner, self.campaign, self.project, self.model = owner, campaign, project, model
        self.receipt, self.protocol = Path(receipt), protocol
        self.query, self.wait, self.monotonic = query, wait, monotonic
        self.window_seconds = float(window_seconds)
        self.initial_delay_seconds = float(initial_delay_seconds)
        self.max_delay_seconds = float(max_delay_seconds)
        self.calls, self.attempts, self.query_errors, self.wait_seconds = [], {}, {}, []
        self.verified = False
        if self.receipt.exists():
            prior = read_json(self.receipt)
            if (
                prior.get("schema_version") != 1
                or prior.get("protocol") != protocol
                or prior.get("status") != "verified"
                or prior.get("campaign") != campaign
                or prior.get("project") != project
                or prior.get("model") != model
                or prior.get("sample_count") != 2
                or prior.get("extra_model_requests") != 0
                or len(prior.get("rows", [])) != 2
                or len({row.get("trace_id") for row in prior.get("rows", [])}) != 2
            ):
                raise ReadbackError("existing first-two readback receipt differs")
            self.verified = True

    def _match(self, call):
        root, task_id, arm, stage, usage = call
        self.attempts[root] = self.attempts.get(root, 0) + 1
        try:
            rows = self.query(root)
        except Exception:
            self.query_errors[root] = self.query_errors.get(root, 0) + 1
            return None
        matches = matching_generation_rows(
            rows,
            root,
            campaign=self.campaign,
            project=self.project,
            model=self.model,
            arm=arm,
            stage=stage,
            task_id=task_id,
            usage=usage,
        )
        if len(matches) > 1:
            raise ReadbackAmbiguous("multiple observations match one generation trace")
        return matches[0] if matches else None

    def __call__(self, root, response, *, task_id, arm, stage):
        if self.verified:
            return
        if len(self.calls) >= 2:
            raise ReadbackError("first-two readback was not verified; no later dispatch")
        self.calls.append((root, task_id, arm, stage, checked_usage(dict(response.usage or {}))))
        if len(self.calls) < 2:
            return
        if self.calls[0][0] == self.calls[1][0]:
            raise ReadbackError("first two traces must be distinct")
        self.owner.flush_and_check()
        started = self.monotonic()
        deadline = started + self.window_seconds
        found, delay = {}, self.initial_delay_seconds
        while True:
            for call in self.calls:
                if call[0] not in found:
                    row = self._match(call)
                    if row is not None:
                        found[call[0]] = row
            if len(found) == 2:
                break
            now = self.monotonic()
            if now >= deadline:
                failure = {
                    "schema_version": 1,
                    "protocol": self.protocol,
                    "status": "failed",
                    "reason": "readback_timeout",
                    "campaign": self.campaign,
                    "project": self.project,
                    "model": self.model,
                    "sample_count": 2,
                    "extra_model_requests": 0,
                    "attempts_per_trace": dict(self.attempts),
                    "query_errors_per_trace": dict(self.query_errors),
                    "rows": list(found.values()),
                }
                failed_path = self.receipt.with_name(self.receipt.stem + ".failed.json")
                if not failed_path.exists():
                    write_json(failed_path, failure)
                raise ReadbackTimeout("bounded first-two trace polling window expired")
            pause = min(delay, deadline - now)
            self.wait(pause)
            self.wait_seconds.append(pause)
            delay = min(self.max_delay_seconds, delay * 2)
        finished = self.monotonic()
        receipt = {
            "schema_version": 1,
            "protocol": self.protocol,
            "status": "verified",
            "campaign": self.campaign,
            "project": self.project,
            "model": self.model,
            "sample_count": 2,
            "extra_model_requests": 0,
            "window_seconds": self.window_seconds,
            "elapsed_seconds": finished - started,
            "backoff": {
                "kind": "exponential",
                "initial_seconds": self.initial_delay_seconds,
                "max_seconds": self.max_delay_seconds,
                "waits_seconds": list(self.wait_seconds),
            },
            "attempts_per_trace": dict(self.attempts),
            "query_errors_per_trace": dict(self.query_errors),
            "one_observation_per_trace": True,
            "same_campaign_session": True,
            "distinct_traces": True,
            "rows": [found[call[0]] for call in self.calls],
        }
        write_json(self.receipt, receipt)
        self.verified = True


class FollowupPaidStage:
    """One durable paid call per opaque identity, with no replay path."""

    def __init__(self, owner, ledger, *, output, verify, wall_deadline, request_timeout):
        if not owner.required:
            raise ValueError("all follow-up calls require observation")
        self.owner, self.ledger, self.output = owner, ledger, Path(output)
        self.verify, self.wall_deadline, self.request_timeout = verify, wall_deadline, request_timeout

    def check(self):
        self.verify()
        if (self.output / "STOPPED.json").exists():
            raise BudgetStop("campaign has a durable stop record")
        if time.time() + self.request_timeout > self.wall_deadline:
            raise BudgetStop("study wall cap leaves no full request deadline")
        if token_state(self.ledger.snapshot())["unknown_or_inflight"]:
            raise BudgetStop("unknown/inflight usage prevents dispatch")
        if self.ledger.campaign_status()["blocked"]:
            raise BudgetStop("campaign ledger is blocked")
        self.owner.check_health()

    def call(self, run_id, messages, *, arm, stage, task_id, max_tokens):
        safe_identifier(run_id)
        safe_identifier(arm)
        safe_identifier(stage)
        safe_identifier(task_id)
        self.check()
        state_path = self.output / "states" / (run_id + ".json")
        if state_path.exists() or any(row["run_id"] == run_id for row in self.ledger.snapshot()["requests"]):
            raise BudgetStop("existing paid slot must not be repeated")
        self.ledger.register_run(run_id, max_requests=1, time_cap=self.request_timeout)
        write_json(
            state_path,
            {
                "status": "started",
                "run_id": run_id,
                "arm": arm,
                "stage": stage,
                "task_id": task_id,
                "started_at": time.time(),
            },
        )
        started = time.monotonic()
        response = self.owner.chat(
            self.ledger,
            run_id,
            [Message(**message) for message in messages],
            arm=arm,
            stage=stage,
            task_id=task_id,
            max_tokens=max_tokens,
            temperature=0,
        )
        private = self.output / "private" / "calls" / (run_id + ".json")
        write_json(
            private,
            {
                "content": response.content,
                "finish_reason": response.finish_reason,
                "usage": response.usage,
                "content_sha256": sha(response.content),
                "latency_seconds": time.monotonic() - started,
                "run_id": run_id,
                "arm": arm,
                "stage": stage,
            },
        )
        write_json(
            state_path,
            {
                "status": "settled",
                "run_id": run_id,
                "arm": arm,
                "stage": stage,
                "task_id": task_id,
                "private_result_sha256": file_sha(private),
            },
            replace=True,
        )
        return response


def safe_identifier(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("opaque identifier must be a nonempty bounded string")
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"
    if any(character not in allowed for character in value):
        raise ValueError("opaque identifier contains unsupported characters")
    return value


def mean(rows, key):
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return sum(values) / len(values) if values else None


def usage_sum(rows, key):
    result = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    covered = 0
    for row in rows:
        usage = row.get(key)
        if not isinstance(usage, dict):
            continue
        values = [usage.get(name) for name in result]
        if any(type(value) is not int or value < 0 for value in values):
            continue
        covered += 1
        for name, value in zip(result, values):
            result[name] += value
    return {**result, "covered_requests": covered, "expected_requests": len(rows)}


def exact_mcnemar(left_only, right_only):
    discordant = left_only + right_only
    if discordant == 0:
        return 1.0
    tail = min(left_only, right_only)
    probability = 2 * sum(math.comb(discordant, index) for index in range(tail + 1)) / (2**discordant)
    return min(1.0, probability)


def paired_comparisons(rows, arms, comparisons):
    by_question = defaultdict(dict)
    for row in rows:
        by_question[row["question_id"]][row["arm"]] = bool(row["correct"])
    result = {}
    for left, right in comparisons:
        counts = {
            "left_only_correct": 0,
            "right_only_correct": 0,
            "both_correct": 0,
            "both_incorrect": 0,
            "compared_pairs": 0,
        }
        for values in by_question.values():
            if not set(arms).issubset(values):
                continue
            a, b = values[left], values[right]
            counts["compared_pairs"] += 1
            if a and b:
                counts["both_correct"] += 1
            elif a:
                counts["left_only_correct"] += 1
            elif b:
                counts["right_only_correct"] += 1
            else:
                counts["both_incorrect"] += 1
        counts["discordant"] = counts["left_only_correct"] + counts["right_only_correct"]
        counts["paired_delta_left_minus_right"] = counts["left_only_correct"] - counts["right_only_correct"]
        counts["mcnemar_exact_p_two_sided"] = exact_mcnemar(
            counts["left_only_correct"], counts["right_only_correct"]
        )
        result[left + "__vs__" + right] = counts
    return result


def request_usage(requests):
    total = {"requests": len(requests), "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "latency": 0.0}
    for row in requests:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("ledger contains unknown usage")
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(name)
            if type(value) is not int or value < 0:
                raise ValueError("ledger usage is incomplete")
            total[name] += value
        created, finished = row.get("created_at"), row.get("finished_at")
        if isinstance(created, (int, float)) and isinstance(finished, (int, float)):
            total["latency"] += max(0.0, finished - created)
    return total
