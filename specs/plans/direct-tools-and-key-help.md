# Plan — two host surfaces that stop one step short

**Status: source-bound on `21a4d2eba`.** Two unrelated fixes, one release. Both
are additive: no signature changes, no removals, no schema change.

Downstream: dinary is blocked on the first and degraded by the second.
`echo-words` is unaffected by either — it constructs `AsyncDirectClient` by
keyword and streams from it (both untouched), and it already reads
`PendingKey.help` from a file-backed registry, where the help is present today.

## 1. A direct call can carry tools

**Now.** `direct()` hands back a client whose verbs are `ask` and `stream`;
neither takes `tools`, and `DirectResult` has no `tool_calls`. The shipped tool
loop accepts a broker and nothing else. So driving one named model through a tool
loop is not expressible — which is requirement 4 of the mission meeting the one
thing the mission says ships above a single call.

**The value is already there and is discarded.** `chat.py::build_chat_request`
takes `tools` and writes `tools`/`tool_choice` into the body;
`chat.py::completion_from_response` returns `(content, tool_calls, usage)`;
`chat.py::_parse_completion` already admits a reply that is tool calls and no
content. `direct.py::_result` binds the middle value to `_tool_calls` and drops
it. `tool_loop.py::_advance_tool_loop` reads only `.text` and `.tool_calls`, so
it is already generic — only its annotation is not.

**Do:**

1. `direct.py`: `DirectResult` gains `tool_calls: list[dict] | None = None`;
   `_result` stops discarding it.
2. `direct.py`: `AsyncDirectClient.chat(messages, *, tools=None, timeout=None,
   params=None) -> DirectResult` and the blocking `DirectClient.chat(...)`, both
   through the existing `_request` / `build_chat_request` path. `ask` unchanged.
3. `protocols/`: a chat port with that one method; `tool_loop.py` annotates its
   `llms` parameter with it instead of `AsyncBroker` / `Broker`, and
   `_advance_tool_loop`'s `AsyncResult | Result` widens the same way. The loop
   body does not change.

**Do not:** give `stream()` tools (`AsyncBroker.stream` has none either — tools
are non-streaming here); journal a direct call (that is
[`caller-visibility.md`](caller-visibility.md)); add `llm_name` to
`DirectResult` (the caller named the model); touch `ask(messages=…)`.

**Tests.** `test_direct.py`: tools reach the body with `tool_choice`; `tool_calls`
come back; a tool-calls-only reply is not an empty answer. `test_tool_loop.py`:
both loops drive a direct client end to end, `ToolLoopLimitError` still fires,
and a loop over a direct client writes no journal row.

## 2. Key acquisition help reaches every backend

**Now.** Same library, same curated list, same `snapshot()` call:

| registry | `missing_keys[].help` |
|---|---|
| zero-config, or a file `Registry` | the curated text |
| `sqlite://`, `postgresql://`, `mongodb://` | `""` |

Both rows measured. Mission requirement 6 names "which keys are missing" as a
host-UI fact and requirement 9 names the database backends as batteries; the
backend most hosts deploy is the one that loses the fact.

**Why.** `catalog.py::Catalog.key_help` reads `self._key_info`, filled in
`_reconcile` from `registry.key_info()` only when the registry implements
`KeyInfoProtocol`. `standalone.registry.Registry` does; `backends.ports
::DriverRegistry` does not. The text is in the process anyway on every sync —
`merge.py::load_sync_source` parses the preset's `[keys]`, `merge.py
::_pending_keys` puts it in `SyncReport.pending_keys` — and then
`refresher.py::_registry_target` persists the configs alone.

**Do:** add a middle step to the precedence `key_help` already implements.

1. `broker/catalog.py`: `Catalog.__init__` takes a pool key-help lookup,
   defaulting to none; `key_help` consults it between the registry's map and the
   declared overlay, under the existing missing-key guard in `_reconcile` — so a
   fully-keyed installation reads nothing.
2. `broker/broker.py`: build that lookup at the `Catalog(...)` site from the
   `PresetSource` and resolved `source` already held there, reading
   `PresetSource.text(source, prefer_cache=True, fetch=False)` — the copy on this
   machine, cache then wheel, never the network. `None` where the installation
   follows no preset.

**Do not:** persist `[keys]` in the registry (a `SCHEMA_VERSION` bump, and every
database host resets its tables to gain documentation that is a pure function of
the curated source); stash the arriving keys at sync time instead (an
installation with `sync_interval=None` that never syncs would get nothing).
`SyncReport.pending_keys` is untouched — it already carries the help.

**Tests.** `test_catalog.py`, `test_registry_keys.py`: a sqlite-backed broker
with an unresolvable ref reports the curated help; a file-backed one is
unchanged; a registry implementing `key_info()` still wins; `sync=None` yields
`""`; a fully-keyed installation reads no preset; `direct_missing_keys` gets help
by the same path.

## Reference and docs

- `rules/direct-by-name.md`: a direct call may carry tools, and still routes
  nothing, fails over to nothing and journals nothing.
- `rules/model-list.md`, "Key acquisition help": the help follows the list the
  installation follows, whatever the registry is made of; the registry capability
  is an override, not the only source.
- `docs/src/en/direct.md`, `tools.md`, `secrets.md` and their `ru` mirrors.
  `tools.md`'s example prints `reply.llm_name`, which a direct reply does not
  carry.
- No `decisions.md` entries: the alternatives worth naming are the "Do not"
  lines above, next to what they explain.
