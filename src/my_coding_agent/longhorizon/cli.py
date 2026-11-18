"""Local CLI for durable Long-horizon MEA runs."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from my_agent_llm import Config, LLM
from my_agent_llm.auth.manager import AuthManager
from my_agent_llm.auth.schema import ApiKeyCredential
from my_agent_llm.observation_export import BoundedExporter, JsonlSink
from my_agent_llm.observation_runtime import ObservationRuntime, create_observation_runtime
from my_coding_agent.paths import AgentPaths
from my_coding_agent.settings import load_settings

from .roles import LLMRoleBackend
from .state import pending_follow_ups, reduce_events
from .store import LongHorizonStore

DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_RUNS_ROOT = AgentPaths().home / "longhorizon" / "runs"


class CLIError(RuntimeError):
    """A safe, user-facing CLI failure."""


def _path_arg(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _validate_resume_pause_after_round(
    state: Mapping[str, Any],
    pause_after_round: int | None,
) -> None:
    if pause_after_round is None:
        return
    if type(pause_after_round) is not int or pause_after_round < 1:
        raise CLIError("pause_after_round must be a positive integer or None")
    completed_rounds = state.get("completed_rounds", [])
    if not isinstance(completed_rounds, list) or any(
        type(round_index) is not int for round_index in completed_rounds
    ):
        raise CLIError("store integrity completed_rounds are invalid")
    if completed_rounds and pause_after_round <= max(completed_rounds):
        raise CLIError(
            "pause_after_round must be later than the last completed round"
        )


def _workspace_relative_paths(
    values: Sequence[str | Path],
    *,
    workspace: Path,
) -> list[str]:
    workspace = workspace.expanduser()
    relative_paths: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            raise CLIError("immutable path must not be empty")
        path = Path(text).expanduser()
        if path.drive and not path.is_absolute():
            raise CLIError("immutable path must not be drive-relative")
        if ".." in path.parts:
            if not path.is_absolute() and path.parts[0] == "..":
                raise CLIError("immutable path escapes workspace")
            raise CLIError("immutable path must not contain '..'")
        if path.is_absolute():
            try:
                relative = path.relative_to(workspace)
            except ValueError as exc:
                raise CLIError("immutable path escapes workspace") from exc
        else:
            relative = path
        relative_paths.append(relative.as_posix())
    return relative_paths


def _read_text_value(value: str, *, label: str) -> str:
    if not value:
        raise CLIError(f"{label} must not be empty")
    if value.startswith("@"):
        path = Path(value[1:]).expanduser()
        if len(value) == 1 or not path.is_file():
            raise CLIError(f"{label} file is missing")
        try:
            return path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise CLIError(f"{label} file is not readable UTF-8") from exc
    return value


def _credential(
    args: argparse.Namespace,
    *,
    provider: str,
    auth_manager_factory: Callable[[], Any],
) -> tuple[str, str | None]:
    auth_provider = getattr(args, "auth_provider", None) or provider
    profile = getattr(args, "auth_profile", None)
    credential_env = getattr(args, "credential_env", None)
    if credential_env:
        key = os.environ.get(credential_env)
        if not key:
            raise CLIError("configured credential environment is unavailable")
        return key, None
    manager = auth_manager_factory()
    credential = manager.get_credential(auth_provider, profile=profile)
    if not isinstance(credential, ApiKeyCredential):
        raise CLIError("AuthManager has no API-key credential for the requested profile")
    try:
        key = credential.resolve_key()
    except Exception as exc:
        raise CLIError("AuthManager credential could not be resolved") from exc
    if not key:
        raise CLIError("AuthManager credential resolved to an empty key")
    return key, credential.base_url


def _same_endpoint(left: str | None, right: str | None) -> bool:
    return (left or "").rstrip("/") == (right or "").rstrip("/")


def _safe_model(
    args: argparse.Namespace,
    *,
    frozen: Mapping[str, Any] | None = None,
    credential_base_url: str | None = None,
) -> dict[str, Any]:
    if frozen is not None:
        for key in ("provider", "model", "base_url", "temperature", "timeout", "max_tokens"):
            requested = getattr(args, key, None)
            if requested is not None and requested != frozen.get(key):
                if key == "base_url":
                    raise CLIError("resume model endpoint does not match frozen manifest")
                raise CLIError(f"resume model {key} does not match frozen manifest")
        candidate_base_url = frozen.get("base_url")
    else:
        candidate_base_url = args.base_url
    if credential_base_url is not None:
        if candidate_base_url is not None and not _same_endpoint(candidate_base_url, credential_base_url):
            raise CLIError("credential model endpoint does not match requested endpoint")
        candidate_base_url = credential_base_url
    model = {
        "provider": frozen.get("provider") if frozen is not None else args.provider,
        "model": frozen.get("model") if frozen is not None else args.model,
        "base_url": candidate_base_url,
        "temperature": frozen.get("temperature") if frozen is not None else args.temperature,
        "timeout": frozen.get("timeout") if frozen is not None else args.timeout,
        "max_tokens": frozen.get("max_tokens") if frozen is not None else args.max_tokens,
        "max_retries": 0,
    }
    if not isinstance(model["provider"], str) or not model["provider"].strip():
        raise CLIError("provider must be nonempty")
    if not isinstance(model["model"], str) or not model["model"].strip():
        raise CLIError("model must be nonempty")
    return model


def _default_controller_factory(*, store: LongHorizonStore, backend: Any):
    try:
        from .engine import LongHorizonController
    except ImportError as exc:
        raise CLIError("LongHorizonController is unavailable") from exc
    return LongHorizonController(store=store, backend=backend)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class _CloseLedger:
    """Close each owned object once while continuing after individual failures."""

    def __init__(self) -> None:
        self._closed: set[int] = set()

    async def close(self, resource: Any) -> bool:
        if resource is None:
            return True
        key = id(resource)
        if key in self._closed:
            return True
        self._closed.add(key)
        close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
        if not callable(close):
            return True
        result = close()
        if inspect.isawaitable(result):
            result = await result
        return result is not False


async def _close_observer_resource(
    name: str,
    observer: Any,
    ledger: _CloseLedger,
) -> tuple[dict[str, Any], bool]:
    report: dict[str, Any] = {"resource": name, "closed": True}
    try:
        report["closed"] = await ledger.close(observer)
    except BaseException as exc:
        report["closed"] = False
        report["error_type"] = type(exc).__name__
    for key in ("dropped", "export_errors", "close_errors"):
        value = getattr(observer, key, 0)
        report[key] = value if type(value) is int and value >= 0 else -1
    alive = getattr(observer, "is_alive", False)
    report["alive"] = bool(alive) if isinstance(alive, bool) else False
    ok = (
        report["closed"] is True
        and report["dropped"] == 0
        and report["export_errors"] == 0
        and report["close_errors"] == 0
        and report["alive"] is False
    )
    return report, ok


async def _close_llm_provider(
    llm: Any,
    ledger: _CloseLedger,
) -> tuple[dict[str, Any], bool]:
    provider = getattr(llm, "_provider", None)
    clients: list[tuple[str, Any]] = []
    for name in ("async_client", "client"):
        client = getattr(provider, name, None)
        if client is not None and all(client is not old for _, old in clients):
            clients.append((name, client))
    reports: list[dict[str, Any]] = []
    ok = True
    for name, client in clients:
        report: dict[str, Any] = {"resource": f"llm.{name}", "closed": True}
        try:
            report["closed"] = await ledger.close(client)
        except BaseException as exc:
            report["closed"] = False
            report["error_type"] = type(exc).__name__
        reports.append(report)
        ok = ok and report["closed"] is True
    return {"resource": "llm_provider", "clients": reports}, ok


async def _close_owned_resources(
    *,
    controller: Any = None,
    cloud: Any = None,
    local: Any = None,
    llm: Any = None,
) -> tuple[dict[str, Any], bool]:
    """Run every close callback in a fixed order; failures never short-circuit."""

    ledger = _CloseLedger()
    reports: list[dict[str, Any]] = []
    ok = True
    report: dict[str, Any] = {"resource": "controller", "closed": True}
    try:
        report["closed"] = await ledger.close(controller)
    except BaseException as exc:
        report["closed"] = False
        report["error_type"] = type(exc).__name__
    if controller is not None:
        reports.append(report)
        ok = ok and report["closed"] is True
    for name, observer in (("cloud_observer", cloud), ("local_observer", local)):
        if observer is None:
            continue
        observer_report, observer_ok = await _close_observer_resource(
            name, observer, ledger
        )
        reports.append(observer_report)
        ok = ok and observer_ok
    try:
        provider_report, provider_ok = await _close_llm_provider(llm, ledger)
    except BaseException as exc:
        provider_report = {
            "resource": "llm_provider",
            "closed": False,
            "error_type": type(exc).__name__,
            "clients": [],
        }
        provider_ok = False
    reports.append(provider_report)
    ok = ok and provider_ok
    return {"ok": ok, "resources": reports}, ok


def _scrub_process_credentials(credential_env: str | None) -> None:
    names = {"LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"}
    if credential_env:
        names.add(credential_env)
    for name in names:
        os.environ.pop(name, None)


def _state_payload(store: LongHorizonStore) -> dict[str, Any]:
    state = reduce_events(store.manifest, store.events())
    return asdict(state)


def _integrity_snapshot(
    store: LongHorizonStore,
) -> tuple[dict[str, Any], dict[str, Any]]:
    value = store.inspect_integrity()
    if not isinstance(value, Mapping):
        raise CLIError("store integrity result is invalid")
    state = value.get("state")
    if not isinstance(state, Mapping):
        raise CLIError("store integrity state is unavailable")
    normalized = dict(state)
    if normalized.get("status") not in {"ready", "waiting_input", "blocked", "complete"}:
        raise CLIError("store integrity state is invalid")
    return _json_safe(dict(value)), normalized


def _attach_local_observation(llm: Any, run_root: Path) -> ObservationRuntime:
    calls_path = run_root / "calls.jsonl"
    calls_path.parent.mkdir(parents=True, exist_ok=True)
    calls_path.touch(exist_ok=True)
    runtime = ObservationRuntime(
        BoundedExporter(JsonlSink(calls_path), capacity=1024)
    )
    try:
        runtime.attach(llm)
    except BaseException:
        runtime.close()
        raise
    return runtime


def _attach_langfuse(
    llm: Any,
    *,
    workspace: Path,
) -> ObservationRuntime:
    settings = load_settings(cwd=workspace).observation
    public_key = os.environ.get(settings.public_key_env)
    secret_key = os.environ.get(settings.secret_key_env)
    base_url = (
        os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or settings.base_url
    )
    if not public_key or not secret_key:
        raise CLIError("Langfuse credentials are unavailable; local calls.jsonl remains active")
    runtime = create_observation_runtime(
        enabled=True,
        public_key=public_key,
        secret_key=secret_key,
        base_url=base_url,
        capacity=settings.capacity,
        network_timeout=settings.network_timeout,
    )
    if runtime is None:
        raise CLIError("Langfuse observation runtime could not be created")
    try:
        runtime.attach(llm)
    except BaseException:
        runtime.close()
        raise
    return runtime


async def _with_observers(
    *,
    llm: Any,
    run_root: Path,
    workspace: Path,
    langfuse: bool,
) -> tuple[dict[str, Any], ObservationRuntime, ObservationRuntime | None]:
    local = _attach_local_observation(llm, run_root)
    cloud = None
    try:
        if langfuse:
            cloud = _attach_langfuse(llm, workspace=workspace)
        status = {
            "local": "recording",
            "cloud": "configured_unverified" if cloud is not None else "disabled",
        }
        return status, local, cloud
    except BaseException as failure:
        _cleanup, cleanup_ok = await _close_owned_resources(
            cloud=cloud,
            local=local,
        )
        if not cleanup_ok:
            raise CLIError("observation setup failed and cleanup was incomplete") from None
        raise failure.with_traceback(failure.__traceback__)


async def _make_backend(
    *,
    args: argparse.Namespace,
    store: LongHorizonStore,
    model: Mapping[str, Any],
    llm_factory: Callable[[Config], Any],
    auth_manager_factory: Callable[[], Any],
) -> tuple[Any, Any, dict[str, Any], ObservationRuntime, ObservationRuntime | None]:
    provider = str(model["provider"])
    key, credential_base_url = _credential(
        args,
        provider=provider,
        auth_manager_factory=auth_manager_factory,
    )
    model_with_endpoint = _safe_model(
        args,
        frozen=model,
        credential_base_url=credential_base_url,
    )
    config = Config(
        provider=str(model_with_endpoint["provider"]),
        model=str(model_with_endpoint["model"]),
        api_key=key,
        base_url=model_with_endpoint["base_url"],
        temperature=model_with_endpoint["temperature"],
        timeout=model_with_endpoint["timeout"],
        max_tokens=model_with_endpoint["max_tokens"],
        max_retries=0,
    )
    llm = None
    local = None
    cloud = None
    try:
        llm = llm_factory(config)
        observation, local, cloud = await _with_observers(
            llm=llm,
            run_root=store.root,
            workspace=store.workspace,
            langfuse=bool(getattr(args, "langfuse", False)),
        )
        _scrub_process_credentials(getattr(args, "credential_env", None))
        backend = LLMRoleBackend(
            llm=llm,
            workspace=store.workspace,
            run_dir=store.root,
            limits=store.manifest["limits"],
        )
        return llm, backend, observation, local, cloud
    except BaseException as failure:
        _cleanup, cleanup_ok = await _close_owned_resources(
            cloud=cloud,
            local=local,
            llm=llm,
        )
        if not cleanup_ok:
            raise CLIError("model setup failed and provider cleanup was incomplete") from None
        raise failure.with_traceback(failure.__traceback__)


def _run_root(args: argparse.Namespace, run_id: str) -> Path:
    return _path_arg(args.runs_root) / run_id


def _structured_result(state: Mapping[str, Any], action: Any = None) -> dict[str, Any]:
    if is_dataclass(action):
        action = asdict(action)
    elif hasattr(action, "to_dict") and callable(action.to_dict):
        action = action.to_dict()
    state_value = dict(state)
    status = state_value.get("status")
    if status not in {"ready", "waiting_input", "blocked", "complete"}:
        status = "blocked"
    return {
        "status": status,
        "state": state_value,
        "result": action,
    }


async def _run_command(
    args: argparse.Namespace,
    *,
    controller_factory: Callable[..., Any],
    llm_factory: Callable[[Config], Any],
    auth_manager_factory: Callable[[], Any],
) -> dict[str, Any]:
    task = _read_text_value(args.task, label="task")
    run_id = args.run_id or uuid4().hex[:16]
    run_root = Path(args.runs_root).expanduser().resolve() / run_id
    workspace = Path(args.workspace).expanduser().resolve()
    immutable_paths = _workspace_relative_paths(
        args.immutable_paths,
        workspace=workspace,
    )
    limits = {
        "max_rounds": args.max_rounds,
        "max_turns": args.max_turns,
        "max_seconds": args.max_seconds,
        "context_budget": args.context_budget,
    }
    key, credential_base_url = _credential(
        args,
        provider=args.provider,
        auth_manager_factory=auth_manager_factory,
    )
    model = _safe_model(args, credential_base_url=credential_base_url)
    store = LongHorizonStore.prepare(
        run_root,
        run_id=run_id,
        task=task,
        workspace=workspace,
        model=model,
        limits=limits,
        immutable_paths=immutable_paths,
    )
    llm = None
    local = None
    cloud = None
    controller = None
    observation: dict[str, Any] = {
        "local": "not_started",
        "cloud": "disabled",
    }
    result: dict[str, Any] | None = None
    failure: BaseException | None = None
    try:
        llm = llm_factory(
            Config(
                provider=model["provider"],
                model=model["model"],
                api_key=key,
                base_url=model["base_url"],
                temperature=model["temperature"],
                timeout=model["timeout"],
                max_tokens=model["max_tokens"],
                max_retries=0,
            )
        )
        observation, local, cloud = await _with_observers(
            llm=llm,
            run_root=store.root,
            workspace=workspace,
            langfuse=args.langfuse,
        )
        _scrub_process_credentials(getattr(args, "credential_env", None))
        controller = controller_factory(
            store=store,
            backend=LLMRoleBackend(
                llm=llm,
                workspace=workspace,
                run_dir=store.root,
                limits=store.manifest["limits"],
            ),
        )
        action = await _maybe_await(
            controller.run_until_terminal(
                pause_after_round=args.pause_after_round,
            )
        )
        result = _structured_result(_state_payload(store), action)
        result.update(
            {
                "run_id": run_id,
                "run_root": str(store.root),
                "observation": observation,
            }
        )
    except BaseException as exc:
        failure = exc
    cleanup, cleanup_ok = await _close_owned_resources(
        controller=controller,
        cloud=cloud,
        local=local,
        llm=llm,
    )
    if failure is not None:
        if not cleanup_ok:
            raise CLIError("run failed and resource cleanup was incomplete") from None
        raise failure.with_traceback(failure.__traceback__)
    if not cleanup_ok:
        raise CLIError("run finished but resource cleanup was incomplete")
    assert result is not None
    if isinstance(result.get("observation"), dict):
        result["observation"]["cleanup"] = cleanup
    return result


async def _resume_command(
    args: argparse.Namespace,
    *,
    controller_factory: Callable[..., Any],
    llm_factory: Callable[[Config], Any],
    auth_manager_factory: Callable[[], Any],
) -> dict[str, Any]:
    run_root = _run_root(args, args.run_id)
    store = LongHorizonStore.load(run_root)
    _integrity, state = _integrity_snapshot(store)
    _validate_resume_pause_after_round(
        state,
        getattr(args, "pause_after_round", None),
    )
    pending = pending_follow_ups(store.manifest, store.events())
    if state["status"] == "complete":
        raise CLIError("completed run cannot be resumed")
    if state["status"] == "blocked":
        raise CLIError("blocked run cannot be resumed")
    if state["status"] == "waiting_input" and not pending:
        result = _structured_result(
            state,
            {
                "resume": "not_needed",
                "reason": "waiting_input_without_pending_follow_up",
            },
        )
        result.update(
            {
                "run_id": store.manifest["run_id"],
                "run_root": str(store.root),
            }
        )
        return result
    frozen_model = store.manifest["model"]
    requested = _safe_model(args, frozen=frozen_model)
    if not _same_endpoint(requested.get("base_url"), frozen_model.get("base_url")):
        raise CLIError("resume model endpoint does not match frozen manifest")
    store.resume_state()
    llm = None
    local = None
    cloud = None
    controller = None
    observation: dict[str, Any] = {
        "local": "not_started",
        "cloud": "disabled",
    }
    result: dict[str, Any] | None = None
    failure: BaseException | None = None
    try:
        llm, backend, observation, local, cloud = await _make_backend(
            args=args,
            store=store,
            model=frozen_model,
            llm_factory=llm_factory,
            auth_manager_factory=auth_manager_factory,
        )
        controller = controller_factory(store=store, backend=backend)
        action = await _maybe_await(
            controller.resume(
                pause_after_round=args.pause_after_round,
            )
        )
        _integrity, final_state = _integrity_snapshot(store)
        result = _structured_result(final_state, action)
        result.update(
            {
                "run_id": store.manifest["run_id"],
                "run_root": str(store.root),
                "observation": observation,
            }
        )
    except BaseException as exc:
        failure = exc
    cleanup, cleanup_ok = await _close_owned_resources(
        controller=controller,
        cloud=cloud,
        local=local,
        llm=llm,
    )
    if failure is not None:
        if not cleanup_ok:
            raise CLIError("resume failed and resource cleanup was incomplete") from None
        raise failure.with_traceback(failure.__traceback__)
    if not cleanup_ok:
        raise CLIError("resume finished but resource cleanup was incomplete")
    assert result is not None
    if isinstance(result.get("observation"), dict):
        result["observation"]["cleanup"] = cleanup
    return result


def _status_command(args: argparse.Namespace) -> dict[str, Any]:
    store = LongHorizonStore.load(_run_root(args, args.run_id))
    snapshot, state = _integrity_snapshot(store)
    return {
        "status": state["status"],
        "run_id": store.manifest["run_id"],
        "run_root": str(store.root),
        "state": state,
        "integrity": {
            "source": "store.inspect_integrity",
            "result": snapshot,
        },
    }


async def _follow_up_command(
    args: argparse.Namespace,
    *,
    controller_factory: Callable[..., Any],
) -> dict[str, Any]:
    store = LongHorizonStore.load(_run_root(args, args.run_id))
    _integrity, initial_state = _integrity_snapshot(store)
    if initial_state.get("status") != "waiting_input":
        raise CLIError("follow-up is accepted only while waiting_input")
    message = _read_text_value(args.message, label="follow-up message")
    controller = None
    failure: BaseException | None = None
    action = None
    try:
        controller = controller_factory(store=store, backend=None)
        action = await _maybe_await(controller.follow_up(message=message))
    except BaseException as exc:
        failure = exc
    _cleanup, close_ok = await _close_owned_resources(controller=controller)
    if failure is not None:
        if not close_ok:
            raise CLIError("follow-up failed and controller cleanup was incomplete") from None
        raise failure.with_traceback(failure.__traceback__)
    if not close_ok:
        raise CLIError("follow-up finished but controller cleanup was incomplete")
    _integrity, final_state = _integrity_snapshot(store)
    return {
        "status": "ok",
        "run_id": store.manifest["run_id"],
        "run_root": str(store.root),
        "state": final_state,
        "result": _json_safe(action),
    }


async def _restore_command(
    args: argparse.Namespace,
    *,
    controller_factory: Callable[..., Any],
) -> dict[str, Any]:
    store = LongHorizonStore.load(_run_root(args, args.run_id))
    controller = None
    failure: BaseException | None = None
    action = None
    try:
        controller = controller_factory(store=store, backend=None)
        action = await _maybe_await(controller.restore(round_index=args.round_index))
    except BaseException as exc:
        failure = exc
    _cleanup, close_ok = await _close_owned_resources(controller=controller)
    if failure is not None:
        if not close_ok:
            raise CLIError("restore failed and controller cleanup was incomplete") from None
        raise failure.with_traceback(failure.__traceback__)
    if not close_ok:
        raise CLIError("restore finished but controller cleanup was incomplete")
    return {
        "status": "ok",
        "run_id": store.manifest["run_id"],
        "run_root": str(store.root),
        "round_index": args.round_index,
        "result": _json_safe(action),
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    raise CLIError("command returned a non-JSON-safe value")


def _add_local_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--json", action="store_true")


def _add_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--auth-provider")
    parser.add_argument("--auth-profile")
    parser.add_argument("--credential-env")


def _add_pause_after_round(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pause-after-round",
        type=_positive_int,
        metavar="N",
        help="stop after N completed rounds and exit in a resumable state",
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="lh-longhorizon")
    commands = result.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="start a new durable Long-horizon MEA run")
    run.add_argument("--task", required=True, help="text or @UTF-8-task-file")
    run.add_argument("--workspace", type=Path, default=Path("."))
    run.add_argument(
        "--immutable-path",
        dest="immutable_paths",
        action="append",
        default=[],
        metavar="PATH",
        help="workspace-relative path or an absolute path inside the workspace",
    )
    run.add_argument("--run-id")
    _add_local_args(run)
    _add_auth_args(run)
    run.add_argument("--provider", default="openai")
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--base-url")
    run.add_argument("--temperature", type=float, default=0.2)
    run.add_argument("--timeout", type=int, default=90)
    run.add_argument("--max-tokens", type=int, default=16384)
    run.add_argument("--max-rounds", type=int, default=8)
    run.add_argument("--max-turns", type=int, default=50)
    run.add_argument("--max-seconds", type=float, default=1800)
    run.add_argument("--context-budget", type=int, default=64000)
    run.add_argument("--langfuse", action="store_true")
    _add_pause_after_round(run)

    status = commands.add_parser("status", help="read durable run state without credentials")
    status.add_argument("run_id")
    _add_local_args(status)

    resume = commands.add_parser("resume", help="resume the next allowed round")
    resume.add_argument("run_id")
    _add_local_args(resume)
    _add_auth_args(resume)
    resume.add_argument("--provider")
    resume.add_argument("--model")
    resume.add_argument("--base-url")
    resume.add_argument("--temperature", type=float)
    resume.add_argument("--timeout", type=int)
    resume.add_argument("--max-tokens", type=int)
    resume.add_argument("--langfuse", action="store_true")
    _add_pause_after_round(resume)

    follow = commands.add_parser("follow-up", help="queue durable operator input")
    follow.add_argument("run_id")
    follow.add_argument("--message", required=True, help="text or @UTF-8-message-file")
    _add_local_args(follow)

    restore = commands.add_parser("restore", help="explicitly restore a checkpoint")
    restore.add_argument("run_id")
    restore.add_argument("--round", dest="round_index", type=int, required=True)
    _add_local_args(restore)
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    controller_factory: Callable[..., Any] | None = None,
    llm_factory: Callable[[Config], Any] | None = None,
    auth_manager_factory: Callable[[], Any] | None = None,
) -> int:
    controller_factory = controller_factory or _default_controller_factory
    llm_factory = llm_factory or LLM
    auth_manager_factory = auth_manager_factory or (
        lambda: AuthManager(auth_path=AgentPaths().auth_path)
    )
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            value = asyncio.run(
                _run_command(
                    args,
                    controller_factory=controller_factory,
                    llm_factory=llm_factory,
                    auth_manager_factory=auth_manager_factory,
                )
            )
        elif args.command == "status":
            value = _status_command(args)
        elif args.command == "resume":
            value = asyncio.run(
                _resume_command(
                    args,
                    controller_factory=controller_factory,
                    llm_factory=llm_factory,
                    auth_manager_factory=auth_manager_factory,
                )
            )
        elif args.command == "follow-up":
            value = asyncio.run(
                _follow_up_command(args, controller_factory=controller_factory)
            )
        else:
            value = asyncio.run(
                _restore_command(args, controller_factory=controller_factory)
            )
    except CLIError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "command": getattr(args, "command", ""),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    except (Exception, KeyboardInterrupt) as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "command": getattr(args, "command", ""),
                    "error": "operation failed",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    safe_value = _json_safe(value)
    print(json.dumps(safe_value, ensure_ascii=False, sort_keys=True))
    if not isinstance(safe_value, Mapping):
        return 2
    status = safe_value.get("status")
    if status == "blocked":
        return 2
    if status not in {"ok", "ready", "waiting_input", "complete"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
