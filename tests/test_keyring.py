"""KeyRing: the keys one caller may pay with — its own scope's first, the shared
ring's otherwise, each read once and held."""

from llmbroker.broker.keyring import KeyRing, known_refs
from llmbroker.standalone.secrets import DictSecrets


class _BlankSecrets:
    """A backend that answers every ref with a blank value (Vault/AWS/DB can)."""

    def __init__(self, value=""):
        self._value = value

    async def resolve(self, ref, user_id=None):
        return self._value


def _rings(mapping, scope="alice"):
    secrets = DictSecrets(mapping)
    shared = KeyRing(secrets)
    return shared, KeyRing(secrets, scope=scope, shared=shared)


async def test_a_scoped_caller_prefers_its_own_key():
    _shared, alice = _rings({"K": "shared", "alice/K": "alice-own"})
    assert await alice.resolve("K") == "alice-own"


async def test_a_scoped_caller_falls_back_to_the_shared_key():
    _shared, alice = _rings({"K": "shared"})
    assert await alice.resolve("K") == "shared"


async def test_an_unscoped_ring_never_reads_a_prefixed_ref():
    shared = KeyRing(DictSecrets({"alice/K": "alice-own"}))
    assert await shared.resolve("K") is None


async def test_a_blank_value_is_no_key_whatever_backend_produced_it():
    for blank in ("", "   \n"):
        ring = KeyRing(_BlankSecrets(blank))
        assert await ring.resolve("K") is None


async def test_a_blank_own_value_falls_back_to_the_shared_ref():
    _shared, alice = _rings({"K": "shared", "alice/K": "  "})
    assert await alice.resolve("K") == "shared"


async def test_payable_names_only_the_refs_a_key_resolves_for():
    ring = KeyRing(DictSecrets({"A": "a"}))
    assert await ring.payable(["A", "B", ""]) == frozenset({"A"})


async def test_a_rejection_outlives_a_rebuild_that_finds_the_same_value():
    """A 401 must not cost a call every time: a rebuild re-reads the ref, and a value
    that has not changed is the same rejected key."""
    ring = KeyRing(DictSecrets({"K": "dead"}))
    assert await ring.payable(["K"]) == frozenset({"K"})

    ring.forget("K")
    assert await ring.payable(["K"]) == frozenset()

    await ring.refresh(None)
    assert await ring.payable(["K"]) == frozenset()


async def test_a_rejection_clears_once_the_value_behind_it_changes():
    """The other half: the rebuild is how an admin who has just stored a working key
    gets it used, without waiting out the refresh clock."""
    secrets = DictSecrets({"K": "dead"})
    ring = KeyRing(secrets)
    await ring.payable(["K"])
    ring.forget("K")

    secrets._mapping["K"] = "fresh"  # noqa: SLF001 - test double, direct mutation
    await ring.refresh(None)
    assert await ring.resolve("K") == "fresh"


async def test_a_rejection_lands_in_the_ring_that_handed_the_value_over():
    """Alice paid with the shared key, so the shared ring is what stops offering it
    — and a caller with a key of its own is untouched."""
    secrets = DictSecrets({"K": "shared", "bob/K": "bob-own"})
    shared = KeyRing(secrets)
    alice = KeyRing(secrets, scope="alice", shared=shared)
    bob = KeyRing(secrets, scope="bob", shared=shared)
    assert await alice.resolve("K") == "shared"
    assert await bob.resolve("K") == "bob-own"

    alice.forget("K")

    assert await alice.resolve("K") is None
    assert await shared.resolve("K") is None
    assert await bob.resolve("K") == "bob-own"


async def test_a_callers_own_dead_key_does_not_fall_through_to_the_shared_one():
    """Alice was given a key of her own, so a rejection of it is hers to fix — putting
    her on the installation's shared quota instead would be a silent bill."""
    secrets = DictSecrets({"K": "shared", "alice/K": "alice-own"})
    shared = KeyRing(secrets)
    alice = KeyRing(secrets, scope="alice", shared=shared)
    assert await alice.resolve("K") == "alice-own"

    alice.forget("K")

    assert await alice.resolve("K") is None
    assert await shared.resolve("K") == "shared"


async def test_the_scope_is_what_the_ring_answers_with():
    shared = KeyRing(DictSecrets({}))
    assert shared.scope is None
    assert KeyRing(DictSecrets({}), scope="alice", shared=shared).scope == "alice"


# ── The enumeration: one listing per rebuild answers which refs exist ────────


class _CountingSecrets:
    """A backend that can list its own refs, and counts what it was asked."""

    def __init__(self, mapping):
        self._mapping = dict(mapping)
        self.reads: list[str] = []
        self.listings = 0

    async def resolve(self, ref):
        self.reads.append(ref)
        return self._mapping[ref]

    async def refs(self, prefix=""):
        self.listings += 1
        return frozenset(r for r in self._mapping if r.startswith(prefix))


async def test_a_held_value_is_read_once_however_often_it_is_asked_for():
    secrets = _CountingSecrets({"A": "a"})
    ring = KeyRing(secrets)
    await ring.refresh(await known_refs(secrets))

    for _ in range(10):
        assert await ring.resolve("A") == "a"
    assert secrets.reads == ["A"]


async def test_one_listing_answers_for_every_caller():
    """The point of the enumeration: a pool of keyless entries must not ask the store
    once per entry per caller — nor list it once per caller."""
    secrets = _CountingSecrets({"A": "a"})
    listing = await known_refs(secrets)
    shared = KeyRing(secrets)
    await shared.refresh(listing)

    for scope in ("alice", "bob", "carol"):
        caller = KeyRing(secrets, scope=scope, shared=shared)
        await caller.refresh(listing)
        assert await caller.payable(["A", "B", "C"]) == frozenset({"A"})

    assert secrets.reads == ["A"]
    assert secrets.listings == 1


async def test_a_ring_built_between_rebuilds_inherits_the_listing():
    """A caller arriving mid-period must not start blind: without the last listing it
    would pay a round trip per ref nobody holds, on its very first call."""
    secrets = _CountingSecrets({"A": "a"})
    listing = await known_refs(secrets)
    shared = KeyRing(secrets)
    await shared.refresh(listing)
    assert await shared.payable(["A"]) == frozenset({"A"})
    secrets.reads.clear()

    newcomer = KeyRing(secrets, scope="alice", shared=shared, known=listing)

    assert await newcomer.payable(["A", "B", "C"]) == frozenset({"A"})
    assert secrets.reads == []


async def test_a_rebuild_re_reads_what_it_holds():
    secrets = _CountingSecrets({"A": "a"})
    ring = KeyRing(secrets)
    await ring.refresh(await known_refs(secrets))
    assert await ring.resolve("A") == "a"

    secrets._mapping["A"] = "rolled"
    await ring.refresh(await known_refs(secrets))
    assert await ring.resolve("A") == "rolled"


async def test_a_listing_that_raises_leaves_the_ring_answering_from_the_store():
    class _NoListing(_CountingSecrets):
        async def refs(self, prefix=""):
            raise OSError("vault is unreachable")

    secrets = _NoListing({"A": "a"})
    ring = KeyRing(secrets)
    await ring.refresh(await known_refs(secrets))
    assert await ring.resolve("A") == "a"


async def test_a_backend_that_cannot_list_is_asked_ref_by_ref():
    assert await known_refs(DictSecrets({"A": "a"})) is None


async def test_a_read_that_raises_is_read_as_unset_rather_than_surfacing():
    class _BrokenRead(_CountingSecrets):
        async def resolve(self, ref):
            raise OSError("secrets backend is unreachable")

    secrets = _BrokenRead({"A": "a"})
    ring = KeyRing(secrets)
    await ring.refresh(await known_refs(secrets))
    assert await ring.resolve("A") is None
