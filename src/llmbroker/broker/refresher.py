"""Keeping the stored model list following the curated one: its own clock, task, check
record and failure policy. Rules in ``specs/reference/rules/model-list.md``."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from llmbroker.broker.catalog import Catalog
from llmbroker.broker.merge import SyncSource, load_sync_source, merge_model_list
from llmbroker.broker.model_list_file import sync_model_list_file
from llmbroker.broker.presets import PAID_CATALOG, PresetSource
from llmbroker.broker.report import format_report
from llmbroker.broker.stamps import stamp_age, write_stamp
from llmbroker.exceptions import SyncRefusedError
from llmbroker.models import LLMConfig, ModelList, SyncReport
from llmbroker.protocols.registry import KeyInfoProtocol, RegistryProtocol
from llmbroker.protocols.store import DisabledMapProtocol, StoreProtocol
from llmbroker.standalone.registry import Registry

logger = logging.getLogger("llmbroker.broker")


class ModelListRefresher:
    """Merges a model list into the registry, and decides when to go looking for one.
    ``source`` is the preset followed, ``None`` for none; ``interval`` is ``None``
    where nothing is fetched on its own; ``live`` says whether a pool is running."""

    def __init__(  # noqa: PLR0913 - it assembles a subsystem: the ports plus its clock
        self,
        registry: RegistryProtocol,
        catalog: Catalog,
        store: StoreProtocol,
        presets: PresetSource,
        *,
        source: str | None,
        interval: float | None,
        home: Path | None,
        declared: Sequence[str | LLMConfig] = (),
        target_label: str | None = None,
        live: Callable[[], bool] = lambda: False,
        rebuild: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._store = store
        self._presets = presets
        self._source = source
        self._interval = interval
        self._home = home
        self._declared = tuple(declared)
        self._target_label = target_label
        self._live = live
        self._rebuild = rebuild

        self._attempted = False
        # Monotonic deadline for the next check; inf until the first one lands.
        self._next_refresh = float("inf")
        self._task: asyncio.Task[None] | None = None
        self.last_report: SyncReport | None = None

    # ------------------------------------------------------------------
    # The two gates
    # ------------------------------------------------------------------

    async def before_provision(self) -> None:
        """Decide what the model list needs before the pool is provisioned: fill an empty
        registry blocking, otherwise arm the clock and refresh off the request path.
        An installation syncing nothing still arms it for the paid catalog."""
        if self._attempted:
            return
        self._attempted = True
        if self._interval is None:
            # No clock at all: the empty registry is not filled either — an
            # installation that fetches nothing gets the error, not a fetch.
            return
        if self._source is None:
            if self._follows_an_alias():
                self._arm(PAID_CATALOG)
            return
        if not await self._registry.load():
            await self._attempt("start")
            return
        self._arm(self._source)

    def schedule(self) -> None:
        """Fire a background refresh when the interval has elapsed. Synchronous by
        design: the hot path pays one monotonic comparison and nothing else."""
        interval = self._interval
        if interval is None:
            return
        if time.monotonic() < self._next_refresh:
            return
        if self._task is not None and not self._task.done():
            return
        # The deadline moves before the task exists, so a burst of concurrent calls
        # schedules one refresh rather than one per call.
        self._next_refresh = time.monotonic() + interval
        self._task = asyncio.create_task(self._attempt("refresh"))

    def _arm(self, source: str) -> None:
        interval = self._interval
        if interval is None:
            return
        age = stamp_age(self._home, self._stamp_key(source))
        self._next_refresh = (
            time.monotonic() + (interval - age) if age is not None and age < interval else 0.0
        )

    async def aclose(self) -> None:
        """Cancel a refresh in flight. The caller closes it before the ports: a
        refresh still running would otherwise write through a registry whose driver
        is closing."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ------------------------------------------------------------------
    # The background path: best-effort, never raising
    # ------------------------------------------------------------------

    async def _attempt(self, reason: str) -> None:
        """Best-effort by construction: neither a start nor a request may fail over a
        model list refresh. The explicit ``sync()`` raises instead."""
        source = self._source
        try:
            if source is None:
                await self._refresh_paid_catalog()
            else:
                await self.sync(source)
        except SyncRefusedError as exc:
            self.last_report = exc.report
            logger.warning("sync %s refused, continuing on the current config: %s", reason, exc)
        except (ValueError, OSError) as exc:
            # What a refresh fails with in normal operation — offline, a throttled
            # CDN, a malformed body, an unwritable target. No traceback to keep.
            logger.warning("sync %s failed, continuing on the current config: %s", reason, exc)
        # A bug here still may not stop the refresh, and a one-line warning off a
        # broad catch is how a bug becomes unreportable — so: traceback.
        except Exception:  # noqa: BLE001 - a refresh may not fail the process
            logger.exception(
                "sync %s failed unexpectedly, continuing on the current config",
                reason,
            )
        finally:
            if self._interval is not None:
                self._next_refresh = time.monotonic() + self._interval
        # The clock is a rebuild trigger in its own right, whatever the merge decided:
        # it is what re-reads a peer's registry edit and re-derives quality daily.
        try:
            await self._rebuild_pool()
        # Inside this task nothing can retrieve an exception, so a re-read that fails
        # has to name the port here or vanish; the pool stays as it is either way.
        except Exception:  # noqa: BLE001 - a detached refresh may not raise
            logger.exception(
                "pool rebuild on the %s check failed, continuing on the current pool",
                reason,
            )

    async def _refresh_paid_catalog(self, *, stamp: bool = True) -> None:
        """Keep the cached paid catalog current on the refresh clock — the only clock
        a declared alias has, and what moves a declared model onto a new version."""
        if not self._follows_an_alias():
            return
        await asyncio.to_thread(self._presets.refresh, PAID_CATALOG)
        if stamp:
            write_stamp(self._home, self._stamp_key(PAID_CATALOG))
        self._catalog.invalidate_declared()

    def _follows_an_alias(self) -> bool:
        return any(isinstance(item, str) for item in self._declared)

    # ------------------------------------------------------------------
    # The check record
    # ------------------------------------------------------------------

    def _stamp_key(self, source: str) -> str:
        """What was checked, and for whom. Keyed by both because two projects on one
        machine have two model lists to keep current, and one project's check must not
        gate the other's."""
        return f"{source} {self._target_identity()}"

    def _target_identity(self) -> str:
        if isinstance(self._registry, Registry):
            return str(self._registry.path.resolve())
        if self._target_label is not None:
            return self._target_label
        # No persistent identity of its own: the home directory is the identity.
        return str(self._home)

    # ------------------------------------------------------------------
    # The explicit sync
    # ------------------------------------------------------------------

    async def sync(self, source: str | None = None) -> SyncReport | None:
        """Merge a model list into the registry and return what it did. With no
        argument: what this installation follows — its preset, or, where it follows
        none, the paid catalog alone, which merges nothing and reports nothing. Raises."""
        target = source if source is not None else self._source
        if target is None:
            await self._refresh_paid_catalog()
            return None
        src = await load_sync_source(target, self._presets)
        current = await self._registry.load()
        # The clock a declared alias rides on, and a catalog nobody can reach may
        # not fail the sync of the model list itself.
        try:
            await self._refresh_paid_catalog(stamp=False)
        except (ValueError, OSError) as exc:
            logger.warning(
                "paid catalog unavailable (%s) — declared models stay on the resolution"
                " already in use",
                exc,
            )
        present = await self._catalog.present_refs(
            [c.api_key_ref for c in (*src.model_list.configs, *current)],
        )
        if isinstance(self._registry, Registry):
            report, changed = await self._file_target(src, self._registry.path, present)
        else:
            report, changed = await self._registry_target(src, current, present)
        if changed and isinstance(self._store, DisabledMapProtocol):
            configs = await self._registry.load()
            await self._store.seed_disabled([c.name for c in configs])
        # Both land before the resync: a resync that raises must not swallow the
        # record of a change already applied.
        if changed:
            logger.info("%s", format_report(report))
        else:
            logger.debug("sync %s: no change", report.source)
        self.last_report = report
        write_stamp(self._home, self._stamp_key(target))
        await self._rebuild_pool()
        return report

    async def _rebuild_pool(self) -> None:
        """A sync is a rebuild trigger, applied or not: a key it has just bootstrapped
        is exactly what a caller is waiting for. Skipped before the pool exists —
        provisioning is its own trigger and runs next."""
        if self._rebuild is not None and self._live():
            await self._rebuild()

    async def _file_target(
        self,
        src: SyncSource,
        target: Path,
        present: frozenset[str],
    ) -> tuple[SyncReport, bool]:
        outcome = sync_model_list_file(target, src, present=present)
        # Outside the identity gate: a key that arrived in the environment is
        # bootstrapped by a sync whether or not the model list itself moved.
        await self._catalog.seed_secrets(list(outcome.configs))
        return outcome.report, outcome.changed

    async def _registry_target(
        self,
        src: SyncSource,
        stored: list[LLMConfig],
        present: frozenset[str],
    ) -> tuple[SyncReport, bool]:
        keys = (
            await self._registry.key_info() if isinstance(self._registry, KeyInfoProtocol) else {}
        )
        outcome = merge_model_list(src, ModelList(configs=stored, keys=keys), present=present)
        merged = outcome.model_list.configs
        # Against what is stored, so a catalog move reaches the registry; keyed by
        # name, since a DB hands rows back in its own order (invariant 3).
        changed = {c.name: c for c in merged} != {c.name: c for c in stored}
        if changed:
            await self._catalog.apply(merged)
        else:
            await self._catalog.seed_secrets(merged)
        return outcome.report, changed
