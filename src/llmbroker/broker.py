"""The async broker core: AsyncBroker, AsyncLLM, AsyncResult, Optimizer, exceptions.

Round-robin failover over a pool of LLM endpoints with per-LLM 429/503 cooldown.
One ``asyncio.Queue`` slot per LLM ⇒ at most one in-flight request per LLM.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from llmbroker.chat import (
    build_chat_request,
    is_rate_limit,
    message_from_response,
    parse_tool_calls,
    parse_usage,
    retry_after_seconds,
)
from llmbroker.models import (
    AsyncResourceProtocol,
    Call,
    CallStatus,
    LLMConfig,
    LLMMetrics,
    LLMSnapshot,
    LLMState,
    SeedPolicy,
    Usage,
)
from llmbroker.registry import MutableRegistryProtocol, RegistryProtocol
from llmbroker.secrets import (
    MutableSecretsProtocol,
    Secrets,
    SecretsProtocol,
    as_secrets,
)
from llmbroker.shared_state import SharedStateProtocol
from llmbroker.state import InMemoryState
from llmbroker.telemetry import (
    QueryableTelemetryProtocol,
    Telemetry,
    TelemetryProtocol,
)

HTTP_429 = 429

logger = logging.getLogger("llmbroker.broker")

_DEFAULT_RATE_LIMIT_SEC = 60
_HTTP_TIMEOUT = 60.0


class LLMRequestError(Exception):
    """Base: this request could not be completed."""


class NoLLMAvailableError(LLMRequestError):
    """No LLM slot came free within ``wait``."""


class AllLLMsFailedError(LLMRequestError):
    """A slot was obtained but every tried LLM errored."""


@dataclass
class Optimizer:
    """P1 shape only — no control loop runs until P4."""

    judge_fraction: float = 0.0


class AsyncResult:
    """Returned by AsyncBroker.ask()/chat()."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        text: str,
        tool_calls: list[dict] | None,
        usage: Usage | None,
        call_id: str,
        llm_name: str,
        telemetry: TelemetryProtocol,
        state: InMemoryState,
    ) -> None:
        self.text = text
        self.tool_calls = tool_calls
        self.usage = usage
        self._call_id = call_id
        self._llm_name = llm_name
        self._telemetry = telemetry
        self._state = state

    async def record_quality(self, score: float) -> None:
        if score == 0.0:
            self._state.record_quality_fail(self._llm_name)
        await self._telemetry.record_quality(self._call_id, score)


class AsyncLLM:
    """Handle returned by ``AsyncBroker[name]`` — live view into broker internals."""

    def __init__(
        self,
        name: str,
        config: LLMConfig,
        state: "InMemoryState",
        telemetry: TelemetryProtocol,
    ) -> None:
        self._name = name
        self._config = config
        self._state = state
        self._telemetry = telemetry

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def state(self) -> LLMState:
        return self._state.get_state(self._name)

    async def metrics(self, *, since: datetime | None = None) -> LLMMetrics:
        if isinstance(self._telemetry, QueryableTelemetryProtocol):
            all_metrics = await self._telemetry.metrics(since=since)
            return all_metrics.get(self._name, LLMMetrics(0, None, None))
        return LLMMetrics(0, None, None)


class AsyncBroker(Mapping[str, AsyncLLM]):
    """Read-only Mapping of LLM handles + the single call/admin front door."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        registry: RegistryProtocol,
        secrets: SecretsProtocol | None = None,
        shared_state: SharedStateProtocol | None = None,
        telemetry: TelemetryProtocol | None = None,
        optimize: bool | Optimizer = True,
        seed: RegistryProtocol | None = None,
        seed_policy: SeedPolicy = SeedPolicy.IF_EMPTY,
    ) -> None:
        self._registry = registry
        self._secrets: SecretsProtocol = as_secrets(secrets) if secrets is not None else Secrets()
        self._shared_state = shared_state
        self._telemetry: TelemetryProtocol = telemetry if telemetry is not None else Telemetry()
        if isinstance(optimize, Optimizer):
            self._optimizer: Optimizer | None = optimize
        elif optimize:
            self._optimizer = Optimizer()
        else:
            self._optimizer = None

        self._seed = seed
        self._seed_policy = seed_policy
        self._queue: asyncio.Queue[LLMConfig] = asyncio.Queue()
        self._configs: dict[str, LLMConfig] = {}
        self._resolved_keys: dict[str, str] = {}
        self._state = InMemoryState()
        self._pool_initialized = False
        self._pool_lock = asyncio.Lock()
        self._bg_tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _populate_pool(self) -> None:
        """Reconcile the in-memory pool with the registry. Assumes _pool_lock is held."""
        configs = await self._registry.load()
        names = {c.name for c in configs}
        for name in list(self._configs):
            if name not in names:
                self._configs.pop(name, None)
                self._resolved_keys.pop(name, None)
        for cfg in configs:
            await self._add_to_pool(cfg)
        self._pool_initialized = True

    async def ensure_pool(self) -> None:
        """Lazy idempotent initializer — seeds and loads the registry into the pool once."""
        if self._pool_initialized:
            return
        async with self._pool_lock:
            if self._pool_initialized:
                return
            if self._seed is not None:
                await self._apply_seed(self._seed, self._seed_policy)
            await self._populate_pool()

    async def _add_to_pool(self, cfg: LLMConfig) -> None:
        is_new = cfg.name not in self._configs
        self._configs[cfg.name] = cfg
        try:
            self._resolved_keys[cfg.name] = await self._secrets.resolve(cfg.api_key_ref)
        except KeyError:
            logger.warning(
                "LLM %s: api_key_ref %r could not be resolved — calls will fail"
                " until the env var / secret is set",
                cfg.name,
                cfg.api_key_ref,
            )
        if is_new:
            self._queue.put_nowait(cfg)

    async def aclose(self) -> None:
        for task in self._bg_tasks:
            task.cancel()
        for task in self._bg_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._bg_tasks.clear()
        for port in (self._registry, self._secrets, self._telemetry, self._shared_state):
            if isinstance(port, AsyncResourceProtocol):
                await port.aclose()

    async def __aenter__(self) -> "AsyncBroker":
        await self.ensure_pool()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Mapping interface
    # ------------------------------------------------------------------

    def __getitem__(self, name: str) -> AsyncLLM:
        if name not in self._configs:
            raise KeyError(name)
        return AsyncLLM(name, self._configs[name], self._state, self._telemetry)

    def __iter__(self) -> Iterator[str]:
        return iter(self._configs)

    def __len__(self) -> int:
        return len(self._configs)

    # ------------------------------------------------------------------
    # Primary role: route a completion
    # ------------------------------------------------------------------

    async def ask(
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        return await self.chat(
            [{"role": "user", "content": prompt}],
            operation=operation,
            trace_id=trace_id,
            wait=wait,
        )

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        await self.ensure_pool()
        while True:
            try:
                config = await self._acquire(wait)
            except (asyncio.QueueEmpty, TimeoutError) as exc:
                raise NoLLMAvailableError("no LLM slot came free within wait") from exc

            if config.name not in self._configs:
                # Removed since enqueued — drop the stale slot, try next.
                continue

            if config.name not in self._resolved_keys:
                self._queue.put_nowait(config)
                raise AllLLMsFailedError(
                    f"{config.name}: api_key_ref {config.api_key_ref!r} not resolved"
                    " — set the env var or configure a secrets backend",
                )
            api_key = self._resolved_keys[config.name]
            call_id = str(uuid.uuid4())
            t0 = time.monotonic()
            status = CallStatus.OK
            http_status: int | None = None
            error_detail: str | None = None
            usage: Usage | None = None
            try:
                content, tool_calls, usage = await self._call_provider(
                    config,
                    api_key,
                    messages,
                    tools,
                )
                self._queue.put_nowait(config)
                self._state.clear_cooling(config.name)
                http_status = 200
                await self._log_call(
                    Call(
                        id=call_id,
                        llm_name=config.name,
                        operation=operation,
                        trace_id=trace_id,
                        status=status,
                        http_status=http_status,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        error_detail=error_detail,
                        usage=usage,
                    ),
                )
                return AsyncResult(
                    text=content,
                    tool_calls=tool_calls,
                    usage=usage,
                    call_id=call_id,
                    llm_name=config.name,
                    telemetry=self._telemetry,
                    state=self._state,
                )
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                http_status = code
                error_detail = exc.response.text[:300]
                if is_rate_limit(code):
                    status = CallStatus.RATE_LIMITED if code == HTTP_429 else CallStatus.UNAVAILABLE
                    await self._cool_down(config, exc.response.headers)
                    await self._log_call(
                        Call(
                            id=call_id,
                            llm_name=config.name,
                            operation=operation,
                            trace_id=trace_id,
                            status=status,
                            http_status=http_status,
                            latency_ms=int((time.monotonic() - t0) * 1000),
                            error_detail=error_detail,
                            usage=usage,
                        ),
                    )
                    if wait == 0:
                        raise NoLLMAvailableError(
                            f"{config.name} rate-limited and wait=0",
                        ) from exc
                    # loop — try the next free LLM / wait out cooldown
                    continue
                status = CallStatus.ERROR
                self._queue.put_nowait(config)
                await self._log_call(
                    Call(
                        id=call_id,
                        llm_name=config.name,
                        operation=operation,
                        trace_id=trace_id,
                        status=status,
                        http_status=http_status,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        error_detail=error_detail,
                        usage=usage,
                    ),
                )
                raise AllLLMsFailedError(f"{config.name} returned HTTP {code}") from exc
            except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
                status = CallStatus.ERROR
                error_detail = type(exc).__name__
                self._queue.put_nowait(config)
                await self._log_call(
                    Call(
                        id=call_id,
                        llm_name=config.name,
                        operation=operation,
                        trace_id=trace_id,
                        status=status,
                        http_status=http_status,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        error_detail=error_detail,
                        usage=usage,
                    ),
                )
                raise AllLLMsFailedError(f"{config.name} network error ({error_detail})") from exc

    async def _acquire(self, wait: float | None) -> LLMConfig:
        if wait is None:
            return await self._queue.get()
        if wait == 0:
            return self._queue.get_nowait()
        return await asyncio.wait_for(self._queue.get(), timeout=wait)

    async def _cool_down(self, config: LLMConfig, headers: httpx.Headers) -> None:
        delay = retry_after_seconds(headers, _DEFAULT_RATE_LIMIT_SEC)
        cooldown_until = datetime.now(UTC) + timedelta(seconds=delay)
        self._state.set_cooling(
            config.name,
            cooldown_until,
            self._state.fail_count(config.name) + 1,
        )
        if self._shared_state is not None:
            await self._shared_state.write(config.name, self._state.get_state(config.name))
        loop = asyncio.get_running_loop()
        loop.call_later(float(delay), self._queue.put_nowait, config)
        logger.warning("LLM %s cooling for %ds", config.name, delay)

    async def _call_provider(
        self,
        config: LLMConfig,
        api_key: str,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> tuple[str, list[dict] | None, Usage | None]:
        url, headers, body = build_chat_request(config, api_key, messages, tools)
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        message = message_from_response(data)
        content = str(message.get("content") or "")
        return content, parse_tool_calls(message), parse_usage(data)

    async def _log_call(self, call: Call) -> None:
        try:
            await self._telemetry.record(call)
        except Exception:  # noqa: BLE001
            logger.exception("llmbroker: telemetry.record failed")

    # ------------------------------------------------------------------
    # Inspection / reporting
    # ------------------------------------------------------------------

    async def snapshot(
        self,
        *,
        since: datetime | None = None,
    ) -> Mapping[str, LLMSnapshot]:
        await self.ensure_pool()
        metrics_map: dict[str, LLMMetrics] = {}
        if isinstance(self._telemetry, QueryableTelemetryProtocol):
            metrics_map = await self._telemetry.metrics(since=since)
        result: dict[str, LLMSnapshot] = {}
        for name, cfg in self._configs.items():
            metrics = metrics_map.get(name) if metrics_map else None
            result[name] = LLMSnapshot(
                config=cfg,
                state=self._state.get_state(name),
                metrics=metrics,
            )
        return result

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def _require_mutable_registry(self) -> MutableRegistryProtocol:
        if not isinstance(self._registry, MutableRegistryProtocol):
            raise TypeError(
                f"{type(self._registry).__name__} does not support mutations"
                " (add/remove/seed require a mutable registry such as"
                " llmbroker.sqlite.Registry)",
            )
        return self._registry

    async def add(self, cfg: LLMConfig) -> None:
        await self.ensure_pool()
        registry = self._require_mutable_registry()
        await registry.add(cfg)
        await self._add_to_pool(cfg)

    async def remove(self, name: str) -> None:
        await self.ensure_pool()
        registry = self._require_mutable_registry()
        await registry.remove(name)
        self._configs.pop(name, None)
        self._resolved_keys.pop(name, None)

    async def _apply_seed(self, source: RegistryProtocol, policy: SeedPolicy) -> None:
        """Apply a seed source to the mutable registry. Assumes _pool_lock is held."""
        registry = self._require_mutable_registry()
        source_configs = await source.load()
        existing = {c.name: c for c in await registry.load()}

        if policy is SeedPolicy.IF_EMPTY and existing:
            return

        if policy is SeedPolicy.MIRROR:
            source_names = {c.name for c in source_configs}
            for name in list(existing):
                if name not in source_names:
                    await registry.remove(name)
            for cfg in source_configs:
                await registry.update(cfg) if cfg.name in existing else await registry.add(cfg)
        else:  # ADD or IF_EMPTY (empty store)
            for cfg in source_configs:
                if cfg.name not in existing:
                    await registry.add(cfg)

        await self._seed_secrets(source_configs)

    async def _seed_secrets(self, configs: list[LLMConfig]) -> None:
        if not isinstance(self._secrets, MutableSecretsProtocol):
            return
        bootstrap = Secrets()
        for cfg in configs:
            try:
                await self._secrets.resolve(cfg.api_key_ref)
                continue  # already resolvable — preserve
            except KeyError:
                pass
            try:
                value = await bootstrap.resolve(cfg.api_key_ref)
            except KeyError:
                continue
            await self._secrets.set(cfg.api_key_ref, value)

    # ------------------------------------------------------------------
    # Call journal / retention
    # ------------------------------------------------------------------

    def _require_queryable(self) -> QueryableTelemetryProtocol:
        if not isinstance(self._telemetry, QueryableTelemetryProtocol):
            raise TypeError(
                "this telemetry backend is not queryable — use a queryable backend"
                " (e.g. llmbroker.sqlite.Telemetry) for calls()/purge_calls()",
            )
        return self._telemetry

    async def calls(self, *, limit: int) -> list[Call]:
        return await self._require_queryable().calls(limit=limit)

    async def purge_calls(self, *, before: datetime) -> int:
        return await self._require_queryable().purge_calls(before=before)

    async def alerts(self) -> list:
        return []
