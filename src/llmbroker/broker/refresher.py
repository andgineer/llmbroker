"""Keeping the stored lineup following the curated one.

Its own clock, its own background task, its own on-disk record of the last check,
and its own failure policy: best-effort on the background path, raising on the
explicit ``sync``. The rules are in ``specs/reference/rules/lineup-refresh.md``.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from llmbroker.broker.aliases import AliasFact, AliasTarget, alias_targets_for
from llmbroker.broker.catalog import Catalog
from llmbroker.broker.keys import KeyProbe
from llmbroker.broker.lineup_file import sync_lineup_file
from llmbroker.broker.merge import SyncSource, load_sync_source, merge_lineup
from llmbroker.broker.presets import PAID_CATALOG, PresetSource
from llmbroker.broker.report import alias_lines, format_report
from llmbroker.broker.source import file_target_path
from llmbroker.broker.stamps import stamp_age, write_stamp
from llmbroker.exceptions import SyncRefusedError
from llmbroker.models import Lineup, LLMConfig, SyncReport
from llmbroker.protocols.registry import KeyInfoProtocol, RegistryProtocol
from llmbroker.protocols.store import DisabledMapProtocol, StoreProtocol
from llmbroker.standalone.registry import Registry

logger = logging.getLogger("llmbroker.broker")


class LineupRefresher:
    """Merges a lineup into the registry, and decides when to go looking for one.

    ``source`` names the lineup this installation follows, ``None`` for a registry
    filled by other means. ``live`` says whether the pool has been provisioned —
    only then does an applied change have a running pool to reconcile.
    """

    def __init__(  # noqa: PLR0913 - it assembles a subsystem: the ports plus its clock
        self,
        registry: RegistryProtocol,
        catalog: Catalog,
        store: StoreProtocol,
        probe: KeyProbe,
        presets: PresetSource,
        *,
        source: str | Path | None,
        interval: float,
        home: Path | None,
        declared: Sequence[str | LLMConfig] = (),
        target_label: str | None = None,
        live: Callable[[], bool] = lambda: False,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._store = store
        self._probe = probe
        self._presets = presets
        self._source = source
        self._interval = interval
        self._home = home
        self._declared = tuple(declared)
        self._target_label = target_label
        self._live = live

        self._attempted = False
        # Monotonic deadline for the next check; inf until the first one lands.
        self._next_refresh = float("inf")
        self._task: asyncio.Task[None] | None = None
        self.last_report: SyncReport | None = None

    # ------------------------------------------------------------------
    # The two gates
    # ------------------------------------------------------------------

    async def before_provision(self) -> None:
        """Decide what the lineup needs before the pool is provisioned.

        An empty registry is filled here, blocking: ``provision()`` on one raises,
        so there is no alternative. A check already on record inside the interval
        needs nothing, and its remainder carries into this process. Otherwise the
        pool is provisioned from what is stored and the refresh runs afterwards, off
        the request path.

        An installation that syncs no lineup still arms the clock when it follows an
        alias: the paid catalog is what that alias resolves through, and it goes
        stale on its own schedule.
        """
        if self._attempted:
            return
        self._attempted = True
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
        if time.monotonic() < self._next_refresh:
            return
        if self._task is not None and not self._task.done():
            return
        # The deadline moves before the task exists, so a burst of concurrent calls
        # schedules one refresh rather than one per call.
        self._next_refresh = time.monotonic() + self._interval
        self._task = asyncio.create_task(self._attempt("refresh"))

    def _arm(self, source: str | Path) -> None:
        age = stamp_age(self._home, self._stamp_key(source))
        self._next_refresh = (
            time.monotonic() + (self._interval - age)
            if age is not None and age < self._interval
            else 0.0
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
        """Best-effort by construction: a refresh that cannot be fetched or cannot
        be applied logs and leaves the running configuration alone. A process must
        not fail to start, and a request must not fail, over a lineup refresh. The
        explicit ``sync()`` call raises instead — that caller has a plan.
        """
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
        # Anything else is a bug, and it still may not stop the refresh: on the
        # background path it would be lost as an unretrieved task exception, and on
        # the start path it would surface as "registry is empty", naming the network
        # for a cause that is not it. Logged with its traceback, because a one-line
        # warning off a broad catch is how a bug becomes unreportable.
        except Exception:  # noqa: BLE001 - a refresh may not fail the process
            logger.exception(
                "sync %s failed unexpectedly, continuing on the current config",
                reason,
            )
        finally:
            self._next_refresh = time.monotonic() + self._interval

    async def _refresh_paid_catalog(self) -> None:
        """Keep the cached paid catalog current on the refresh clock.

        A declared alias resolves through that cache, so without this an
        installation that syncs no lineup would follow whatever version its wheel
        shipped with for as long as the release stayed installed. Where a lineup
        *is* synced this is redundant — ``sync`` reads the catalog itself.
        """
        if not self._follows_an_alias():
            return
        if self._home is not None:
            await asyncio.to_thread(self._presets.refresh, PAID_CATALOG)
            write_stamp(self._home, self._stamp_key(PAID_CATALOG))
        # With nowhere to cache, the resolution below fetches for itself; warming
        # a cache that cannot be written would only fetch the same body twice.
        self._catalog.invalidate_declared()

    def _follows_an_alias(self) -> bool:
        return any(isinstance(item, str) for item in self._declared)

    # ------------------------------------------------------------------
    # The check record
    # ------------------------------------------------------------------

    def _stamp_key(self, source: RegistryProtocol | str | Path) -> str:
        """What was checked, and for whom. Keyed by both because two projects on one
        machine have two lineups to keep current, and one project's check must not
        gate the other's."""
        label = str(source) if isinstance(source, (str, Path)) else type(source).__name__
        return f"{label} {self._target_identity()}"

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

    async def sync(self, source: RegistryProtocol | str | Path) -> SyncReport:
        """Merge a lineup into the registry and return what it did. Raises."""
        src = await load_sync_source(source, self._presets)
        current = await self._registry.load()
        # One place for both targets: an alias-following entry follows the catalog
        # whether this installation keeps its lineup in a file or in a database. A
        # `direct=` alias is stored nowhere and needs no refresh here, but it is
        # what keeps the cached catalog current for the provision that resolves it.
        targets = await alias_targets_for(
            [*(c.alias for c in current), *(d for d in self._declared if isinstance(d, str))],
            self._presets,
        )
        # The read above refreshed the cached catalog, which is where a declared
        # model resolves from: this is the clock it follows.
        self._catalog.invalidate_declared()
        if isinstance(self._registry, Registry):
            report, changed = await self._file_target(
                src,
                file_target_path(self._registry),
                targets,
            )
        else:
            report, changed = await self._registry_target(src, current, targets)
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
        write_stamp(self._home, self._stamp_key(source))
        if changed and self._live():
            await self._catalog.resync()
        return report

    def _log_alias_facts(self, label: str, facts: tuple[AliasFact, ...]) -> None:
        notices, warnings = alias_lines(facts)
        for notice in notices:
            logger.info("sync %s: %s", label, notice)
        for warning in warnings:
            logger.warning("sync %s: %s", label, warning)

    async def _file_target(
        self,
        src: SyncSource,
        target: Path,
        targets: Mapping[str, AliasTarget],
    ) -> tuple[SyncReport, bool]:
        if not src.preset:
            raise ValueError(
                f"cannot sync {src.label} into the file registry {target}: a .toml registry"
                ' takes a curated preset name only (e.g. broker.sync("freetier")). A file or'
                " registry source syncs into a database registry — the vendored-lockfile"
                " deploy path",
            )
        outcome = await sync_lineup_file(
            target,
            src,
            probe=self._probe,
            store=self._store,
            alias_targets=targets,
        )
        # Outside the identity gate: a key that arrived in the environment is
        # bootstrapped by a sync whether or not the lineup itself moved.
        await self._catalog.seed_secrets(list(outcome.configs))
        self._log_alias_facts(src.label, outcome.refresh.facts)
        return outcome.report, outcome.changed

    async def _registry_target(
        self,
        src: SyncSource,
        stored: list[LLMConfig],
        targets: Mapping[str, AliasTarget],
    ) -> tuple[SyncReport, bool]:
        keys = (
            await self._registry.key_info() if isinstance(self._registry, KeyInfoProtocol) else {}
        )
        outcome = await merge_lineup(
            src,
            Lineup(configs=stored, keys=keys),
            probe=self._probe,
            store=self._store,
            alias_targets=targets,
        )
        self._log_alias_facts(src.label, outcome.refresh.facts)
        merged = outcome.lineup.configs
        # Against what is stored, not against the alias-refreshed copy: a catalog
        # move is a change and has to reach the registry. Keyed by name rather than
        # compared as lists — a database registry hands its rows back ordered by
        # name, so position here is the backend's, not the lineup's, and would
        # report every no-op sync as a change.
        changed = {c.name: c for c in merged} != {c.name: c for c in stored}
        if changed:
            await self._catalog.apply(merged)
        else:
            await self._catalog.seed_secrets(merged)
        return outcome.report, changed
