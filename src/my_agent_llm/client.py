# pyright: reportUnreachable=false
"""LLM 门面：按 provider 路由到对应实现，对外一套 API，只透传不碰 SDK。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from threading import Lock
from typing import Any

from .config import Config  # pyright: ignore[reportMissingImports]
from .observation import CallObservation, CallRecord, Observer, ObserverSubscription
from .events import StreamDoneEvent, StreamErrorEvent, StreamEvent  # pyright: ignore[reportMissingImports]
from .models import (  # pyright: ignore[reportMissingImports]
    Message,
    Response,
    StreamChunk,
)
from .providers import Provider  # pyright: ignore[reportMissingImports]
from .providers.registry import (
    PROVIDER_REGISTRY,  # pyright: ignore[reportMissingImports]
)


class LLM:
    """统一 LLM 客户端门面：严格接收强类型 Config 配置，多态路由至具体 Provider。"""

    def __init__(self, config: Config, *, observer: Observer | None = None) -> None:
        """标准工程构造：严格接收 Config 实例。"""
        if not isinstance(config, Config):  # pyright: ignore[reportUnreachable]
            raise TypeError(f"LLM expects a Config instance, got {type(config).__name__}")  # pyright: ignore[reportUnreachable]
        if config.provider not in PROVIDER_REGISTRY:
            raise ValueError(f"Unknown provider '{config.provider}'. Available: {', '.join(sorted(PROVIDER_REGISTRY))}")
        if not config.api_key and config.provider != "antigravity":
            raise ValueError(f"No API key for provider: {config.provider}")
        provider_cls = PROVIDER_REGISTRY[config.provider]
        self._provider: Provider = provider_cls(config)
        self.config = config
        self.observer = observer
        self._observer_lock = Lock()
        self._observer_subscriptions: dict[object, Observer] = {}

    def add_observer(self, observer: Observer) -> ObserverSubscription:
        """Subscribe without replacing or taking ownership of the base observer.

        Removing a subscription affects future calls. Calls already started retain
        their snapshot through their terminal record, including stream close.
        """
        if not callable(observer):
            raise TypeError("observer must be callable")
        token = object()
        with self._observer_lock:
            self._observer_subscriptions[token] = observer

        def remove() -> None:
            with self._observer_lock:
                self._observer_subscriptions.pop(token, None)

        return ObserverSubscription(remove)

    def _observer_snapshot(self) -> Observer | None:
        with self._observer_lock:
            observers = ((self.observer,) if self.observer is not None else ()) + tuple(
                self._observer_subscriptions.values()
            )
        if not observers:
            return None

        def emit(record: CallRecord) -> None:
            for observer in observers:
                try:
                    observer(record)
                except BaseException:
                    # A failed observer must not suppress other subscribers.
                    pass

        return emit

    @property
    def model(self) -> str:
        """当前配置的模型名。"""
        if not self.config.model:
            raise ValueError("No model specified. Pass model=... or set Config.model.")
        return self.config.model

    def _resolve_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """统一合并调用参数与全局 Config 中的默认采样配置。"""
        opts = dict(kwargs)
        if self.config.temperature is not None:
            opts.setdefault("temperature", self.config.temperature)
        if self.config.max_tokens is not None:
            opts.setdefault("max_tokens", self.config.max_tokens)
        return opts

    def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Response:
        """同步对话：完整历史 + 可选工具。"""
        opts = self._resolve_kwargs(kwargs)
        effective_model = model or self.model
        call = CallObservation(self._observer_snapshot(), provider=self.config.provider, model=effective_model, method="chat")
        try:
            with call.scope():
                response = self._provider.chat(messages, model=effective_model, tools=tools, **opts)
            call.update_usage(response.usage)
            call.update_outcome(response.outcome)
            return response
        except BaseException as exc:
            call.fail(exc)
            raise
        finally:
            call.finish()

    def stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Iterator[StreamChunk]:
        """同步流式。"""
        opts = self._resolve_kwargs(kwargs)
        effective_model = model or self.model
        call = CallObservation(self._observer_snapshot(), provider=self.config.provider, model=effective_model, method="stream")
        stream = None
        try:
            with call.scope():
                stream = iter(self._provider.stream(messages, model=effective_model, tools=tools, **opts))
            while True:
                with call.scope():
                    try:
                        chunk = next(stream)
                    except StopIteration:
                        break
                call.update_usage(chunk.response.usage if chunk.response and chunk.response.usage is not None else chunk.usage)
                call.update_outcome(chunk.response.outcome if chunk.response is not None else chunk.outcome)
                yield chunk
        except BaseException as exc:
            call.fail(exc)
            raise
        finally:
            try:
                if stream is not None and hasattr(stream, "close"):
                    with call.scope():
                        stream.close()
            except BaseException as exc:
                if call.status == "ok":
                    call.fail(exc)
                    raise
            finally:
                call.finish()

    async def achat(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Response:
        """异步对话。"""
        opts = self._resolve_kwargs(kwargs)
        effective_model = model or self.model
        call = CallObservation(self._observer_snapshot(), provider=self.config.provider, model=effective_model, method="achat")
        try:
            with call.scope():
                response = await self._provider.achat(messages, model=effective_model, tools=tools, **opts)
            call.update_usage(response.usage)
            call.update_outcome(response.outcome)
            return response
        except BaseException as exc:
            call.fail(exc)
            raise
        finally:
            call.finish()

    async def achat_stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """异步流式。调用方直接 `async for chunk in llm.achat_stream(...)` 迭代，不 await。"""
        opts = self._resolve_kwargs(kwargs)
        effective_model = model or self.model
        call = CallObservation(self._observer_snapshot(), provider=self.config.provider, model=effective_model, method="achat_stream")
        stream = None
        try:
            with call.scope():
                stream = self._provider.achat_stream(messages, model=effective_model, tools=tools, **opts).__aiter__()
            while True:
                with call.scope():
                    try:
                        chunk = await anext(stream)
                    except StopAsyncIteration:
                        break
                call.update_usage(chunk.response.usage if chunk.response and chunk.response.usage is not None else chunk.usage)
                call.update_outcome(chunk.response.outcome if chunk.response is not None else chunk.outcome)
                yield chunk
        except BaseException as exc:
            call.fail(exc)
            raise
        finally:
            try:
                if stream is not None and hasattr(stream, "aclose"):
                    with call.scope():
                        await stream.aclose()
            except BaseException as exc:
                if call.status == "ok":
                    call.fail(exc)
                    raise
            finally:
                call.finish()

    async def astream_events(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        signal: Any | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """异步高阶流式事件流：直接产出 StreamStartEvent/TextDeltaEvent/StreamDoneEvent/StreamErrorEvent。"""
        opts = self._resolve_kwargs(kwargs)
        effective_model = model or self.model
        call = CallObservation(self._observer_snapshot(), provider=self.config.provider, model=effective_model, method="astream_events")
        stream = None
        try:
            with call.scope():
                stream = self._provider.astream_events(
                    messages, model=effective_model, tools=tools, signal=signal, **opts
                ).__aiter__()
            while True:
                with call.scope():
                    try:
                        ev = await anext(stream)
                    except StopAsyncIteration:
                        break
                if isinstance(ev, StreamDoneEvent):
                    call.update_usage(ev.usage)
                    call.update_outcome((ev.message.metadata or {}).get("stop_reason"))
                elif isinstance(ev, StreamErrorEvent):
                    call.status = "cancelled" if ev.stop_reason == "cancelled" else "error"
                    call.error_type = type(ev.exc).__name__ if ev.exc is not None else None
                yield ev
        except BaseException as exc:
            call.fail(exc)
            raise
        finally:
            try:
                if stream is not None and hasattr(stream, "aclose"):
                    with call.scope():
                        await stream.aclose()
            except BaseException as exc:
                if call.status == "ok":
                    call.fail(exc)
                    raise
            finally:
                call.finish()
