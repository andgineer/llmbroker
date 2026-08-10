# The declared-model overlay is not the Catalog's job

**Skeleton — not ready to implement.** Written in full after
`lineup-file-ownership` merges, which changes how a declared model reaches the
catalog text it resolves against. Findings are against today's code.

## Goal

`Catalog` reconciles the registry into the live pool. It also resolves the
models declared with `direct=`, which never enter the pool at all. Separate the
second job, and break the ownership cycle it created.

## Findings

1. **Half the class is about non-pool models.** The docstring says *"keep the
   live pool's membership in sync with the registry"*. The members serving
   `direct=` are: `_overlay`, `_declared`, `_declared_lock`,
   `invalidate_declared`, `_resolve_overlay`, `key_help`,
   `_direct_missing_keys`, `_direct_without_keys`, `_pending`.

2. **Ownership is circular.** `broker.py:283` injects
   `overlay=self._resolve_declared` — a callback into the broker — and the
   broker then calls `catalog.invalidate_declared()` from two places (`sync`
   and `_refresh_paid_catalog`). So: Broker → Catalog → Broker, with the cache
   invalidated from outside the object that holds it.

3. ~~**`entries()` re-reads and re-validates the registry on every `direct()`
   call.**~~ **Closed by `named-models-are-declared`, not by this plan.** A named
   model is only ever declared once that lands, so `direct()` resolves against
   the declared list and never opens the registry. The cache this finding argued
   for is not needed: the per-call file open, TOML parse and uniqueness checks go
   away with the storage. Verify before scoping — and note the one thing that
   must survive without a registry read: `PoolModelError`'s message, which
   distinguishes "that name is a pool member" from "no such model", and can
   answer from the live pool's own configs in memory.

4. **The cache has a lock, a clock and a fallback policy**
   (`_resolve_overlay`, plus `AsyncBroker._resolve_declared`'s rule that the
   first resolution raises and every later one falls back to the resolution in
   use). That is a coherent little subject with its own state — which is the
   argument for it being an object.

## Direction (not yet a route)

A `broker/declared.py` owning the cache, the lock, the key help and its own
invalidation, taking the preset source as a dependency. `Catalog` asks it for
configs; nothing calls back into `AsyncBroker`.

## What changes before this plan is written

`lineup-file-ownership` step 6 replaces the three preset readers and the
`bundled: bool` flag with one source object holding an explicit precedence.
`AsyncBroker._resolve_declared`'s `bundled=previous is None` argument — the
"first resolution may use the wheel's copy, later ones may not" rule — is
expressed through that flag today. **Check how the rule survives step 6 before
scoping**: it may already have a home, or it may have moved intact and still
need one.

`registry-ownership` makes the broker refuse to be built when a registry object
arrives without an explicit `sync`, which any example or test in the real plan
that constructs a broker by hand will have to satisfy.

`one-broker-many-callers` takes the resolved key out of the pool slot and gives
it to the caller, so `Catalog`'s key-facing members in finding 1 —
`_direct_missing_keys`, `_direct_without_keys`, `key_help`'s two readers — ask a
key ring instead of the pool. Re-take that member list from the file: the split
proposed here is unaffected in principle, but which members are on which side of
it is not.

**`named-models-are-declared` is the one that changes this plan's subject, and it
must merge first.** It closes finding 3 outright (above) and raises what is left:
the declared overlay stops being a side feature of `Catalog` and becomes the only
home a named model has, so the split proposed here is separating two halves of
the product rather than tidying one class. It also hands this object more work —
that plan moves the alias facts from the merge to the re-resolution, so whatever
owns the declared models owns producing them too.

## Open questions for the real plan

- Who invalidates? Today two broker call sites do. If the declared resolver
  owns a clock of its own it needs none — but then it needs to know when the
  catalog cache moved, which is the preset source's business.
- `Catalog.key_help` merges the registry's own `[keys]` with the catalog's,
  registry winning. After the split, which side owns that merge? It is read by
  `MissingKeyError`'s message and by `_pending`, so both halves need it.
- `check_overlay` (name/alias collision between declared and stored) is a rule
  about two sources meeting. It belongs wherever they meet, which may be
  neither object.

## Spec updates

`rules/direct-aliases.md` states the alias contract and that declared models are
not stored. Neither changes here — this is a move, not a decision. Confirm the
file still describes the code afterwards.
