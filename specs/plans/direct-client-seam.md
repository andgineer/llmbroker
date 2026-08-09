# The direct clients need a seam, not a private call

**Skeleton — not ready to implement.** Written in full after
`router-failover-and-sse` merges, which removes one of the two duplications
below. Findings are against today's code.

## Goal

Two things: the sync broker reaches a direct client through a private method,
and the two direct clients are copy-paste of each other.

## Findings

1. **`sync.py:203` calls a private method.**

   ```python
   cfg, key = self._run(self._async._resolve_direct(alias, name=name))  # noqa: SLF001
   ```

   Every other method of `sync.Broker` is an honest proxy: submit the coroutine
   to the loop thread, return the result. This one reaches inside
   `AsyncBroker`, then constructs a **separate blocking httpx client that never
   touches the loop thread**. The `noqa` is not a style waiver — it marks a
   missing public seam. The seam is a resolution step: entry plus key, without
   a transport attached.

2. **`AsyncDirectClient` and `DirectClient` duplicate their bodies**
   (`direct.py:94-264`): `__init__`, `_ensure_http`, `_owns_http`, the `ask`
   body and close semantics, differing only by `await`. `DirectClient` exists
   because streaming is async-only and a blocking single POST needs no loop —
   that reason is sound and the class stays; what does not need duplicating is
   everything that is not the transport.

## What changes before this plan is written

`router-failover-and-sse` part 1 moves the SSE validation loop out of
`direct.py` into `chat.py`. `AsyncDirectClient.stream` shrinks accordingly, so
scope this plan against the file as it is *then*, not as described here.

`registry-ownership` changes `sync.Broker.__init__` — its `sync` parameter takes
a sentinel default — so the `sync.py:203` line number above will have moved.
The finding itself is untouched: the private call and the separately
constructed blocking client are both still there.

## Open questions for the real plan

- Shape of the seam: `AsyncBroker.resolve_direct(alias, name=) -> DirectTarget`
  (a frozen config-plus-key value) is the obvious candidate, but it hands a
  resolved secret to the caller. Today `_resolve_direct` does the same thing
  privately — making it public makes it a contract. Is that acceptable, or
  should the seam return a *client factory* instead and keep the key inside?
- Should `sync.Broker.direct()` proxy through the loop thread like everything
  else, or is a genuinely loop-free blocking client the point? Both are
  defensible; today's code does the second by accident rather than by decision,
  and whichever is chosen should be stated.
- Sharing bodies between the two clients: a base class with an abstract
  transport, or a shared module-level request/response pair that both call.
  The second keeps the two classes flat and readable; the first removes more
  lines.

## Spec updates

`rules/direct-aliases.md` names the host-facing contract, so if the seam becomes
public it belongs there — a public entry point is one of the three things a
spec may name.
