"""What a merge site can learn about the keys behind a lineup.

The rule the two types encode — absence of a key is evidence only where the probe
could have found one — is in ``specs/reference/rules/sync-merge.md``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from llmbroker.broker.catalog import resolve_key
from llmbroker.protocols.secrets import SecretsProtocol


@dataclass(frozen=True, slots=True)
class KeyEvidence:
    """The outcome of one probe: which refs a key exists for, and how much a missing
    one proves here. The default proves nothing, which is the conservative reading."""

    present: frozenset[str] = frozenset()
    visible: bool = False
    scoped: bool = False


@dataclass(frozen=True, slots=True)
class KeyProbe:
    """The keys one merge site can see. Built once per merge site, and the only place
    ``scope`` and ``have_keys`` are read."""

    secrets: SecretsProtocol
    scope: str | None = None
    have_keys: bool | Sequence[str] = False

    async def evidence(self, refs: Iterable[str]) -> KeyEvidence:
        present = await self._present(refs)
        return KeyEvidence(
            present=frozenset(present),
            visible=self._visible(present),
            scoped=self.scope is not None,
        )

    async def _present(self, refs: Iterable[str]) -> set[str]:
        """The subset of ``refs`` a key exists for — resolvable, or declared via
        ``have_keys``. A declared ref counts as present only for weighing a removal;
        it never makes a model routable."""
        wanted = {ref for ref in refs if ref}
        if self.have_keys is True:
            return wanted
        declared = declared_refs(self.have_keys)
        present = wanted & declared
        for ref in wanted - present:
            if await resolve_key(self.secrets, ref, self.scope) is not None:
                present.add(ref)
        return present

    def _visible(self, present: set[str]) -> bool:
        """Per-user keys behind ``scope`` are one place a probe cannot prove absence;
        a probe that resolved nothing at all is the other — the keys live in a store
        this merge site cannot reach, and "no key anywhere" is indistinguishable from
        "not my keys"."""
        declared = self.have_keys is True or bool(declared_refs(self.have_keys))
        return bool(present) and (self.scope is None or declared)


def declared_refs(have_keys: bool | Sequence[str]) -> set[str]:
    """The refs ``have_keys`` names outright."""
    if isinstance(have_keys, bool):
        return set()
    # A bare string is a Sequence[str]: taken apart into characters it would declare
    # nothing and silently lose the caller's promise.
    if isinstance(have_keys, str):
        return {have_keys}
    return set(have_keys)
