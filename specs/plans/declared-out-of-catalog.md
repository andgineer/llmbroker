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

3. **`entries()` re-reads and re-validates the registry on every `direct()` call.**
   Its own docstring says that read "must not pay a catalog parse" — and it does
   not, but it does pay a registry one: a file open, a TOML parse and the
   uniqueness checks, synchronously on the event loop, per call. The registry
   deliberately re-reads rather than caches — a cache there would be a staleness
   bug, and that decision is right — so the cache belongs on this side, where a
   sync already knows when to drop it. Whatever object ends up owning the
   declared overlay is the natural owner of a lineup read that a sync
   invalidates.

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

`registry-ownership` changes what a sync may do to a stored entry, not when it
drops a cached read, so finding 3's "a read that a sync invalidates" should
still hold — verify it rather than assume. It also makes the broker refuse to be
built when a registry object arrives without an explicit `sync`, which any
example or test in the real plan that constructs a broker by hand will have to
satisfy.

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
