"""The keys one caller may pay with: resolved values for its own scope, over the
installation's shared ring. See ``rules/backends.md`` for what a scope reaches."""

import logging
from collections.abc import Iterable

from llmbroker.protocols.secrets import EnumerableSecretsProtocol, SecretsProtocol

logger = logging.getLogger("llmbroker.broker")


async def resolve_ref(secrets: SecretsProtocol, ref: str) -> str | None:
    """One backend lookup, with a blank value read as no value at all — any backend
    can hand one back, and a blank key would route real requests at nothing."""
    if not ref:
        return None
    try:
        value = await secrets.resolve(ref)
    except KeyError:
        return None
    return value if value.strip() else None


async def known_refs(secrets: SecretsProtocol) -> frozenset[str] | None:
    """Every ref the store holds, or ``None`` where the backend cannot list — then
    every ref is asked for individually. One listing answers for every caller, so
    this is asked once per rebuild and never per ring."""
    if not isinstance(secrets, EnumerableSecretsProtocol):
        return None
    try:
        return await secrets.refs()
    except Exception:  # noqa: BLE001 - a listing may not fail a call
        logger.exception("secrets backend could not list its refs — asking one by one")
        return None


class KeyRing:
    """The keys of one scope, read once and held until the pool is rebuilt. A scoped
    ring answers from its own values first and falls back to the shared ring, which is
    where a shared key is read and held so every caller shares the one read."""

    def __init__(
        self,
        secrets: SecretsProtocol,
        *,
        scope: str | None = None,
        shared: "KeyRing | None" = None,
        known: frozenset[str] | None = None,
    ) -> None:
        self._secrets = secrets
        self._scope = scope
        self._shared = shared
        self._values: dict[str, str] = {}
        self._missing: set[str] = set()
        self._rejected: dict[str, str] = {}
        # A ring built between rebuilds inherits the last listing rather than starting
        # blind: without it a first-time caller pays a read per ref nobody holds.
        self._known = known

    @property
    def scope(self) -> str | None:
        """Whose keys these are, and what this caller's journal rows are attributed to."""
        return self._scope

    async def resolve(self, ref: str) -> str | None:
        """The key behind ``ref`` for this caller, or ``None`` where none is set or the
        provider has rejected the one this ring held."""
        if ref in self._rejected:
            return None
        if ref in self._values:
            return self._values[ref]
        if ref and ref not in self._missing:
            own = await self._read(self._own_ref(ref))
            if own is not None:
                self._values[ref] = own
                return own
            self._missing.add(ref)
        return await self._shared.resolve(ref) if self._shared is not None else None

    async def payable(self, refs: Iterable[str]) -> frozenset[str]:
        """Which of ``refs`` this caller can pay for — what the pool filters
        candidates on, and what the health measure counts."""
        payable = set()
        for ref in dict.fromkeys(refs):
            if await self.resolve(ref) is not None:
                payable.add(ref)
        return frozenset(payable)

    def forget(self, ref: str) -> None:
        """Drop a value the provider has just rejected, in the ring that handed it
        over, and remember which value it was — that is what a later rebuild compares
        against to tell a replaced key from the same dead one."""
        rejected = self._values.pop(ref, None)
        if rejected is None:
            if self._shared is not None:
                self._shared.forget(ref)
            return
        self._missing.discard(ref)
        self._rejected[ref] = rejected

    async def refresh(self, known: frozenset[str] | None) -> None:
        """Forget every value read and take the rebuild's one listing of the store. A
        rejection survives only while its value does: an admin who has replaced the
        key gets it used, and one who has not is not charged a call to find out."""
        self._values.clear()
        self._missing.clear()
        self._known = known
        self._rejected = {
            ref: value
            for ref, value in self._rejected.items()
            if await self._read(self._own_ref(ref)) == value
        }

    async def _read(self, ref: str) -> str | None:
        """One lookup, skipped entirely for a ref the listing did not name. Anything it
        raises is logged and read as unset: a key nobody can fetch may not fail a call."""
        if not ref or (self._known is not None and ref not in self._known):
            return None
        try:
            return await resolve_ref(self._secrets, ref)
        except Exception:  # noqa: BLE001 - an unreadable key may not fail a call
            logger.exception("secrets backend could not read %r — treating it as unset", ref)
            return None

    def _own_ref(self, ref: str) -> str:
        return f"{self._scope}/{ref}" if self._scope is not None else ref
