# Mission-conformance fixes

Source: a mission-vs-implementation review against `specs/reference/mission.md`
(2026-07). The findings below are ordered by severity; each phase is
independently shippable and ends green on the done gate (`invoke pre` +
`python -m pytest`). Reference specs (`architecture.md`, `optimizer.md`) must be
updated in the same phase as the behavior they describe — they document current
state only.

## Context

The review found one correctness hole and a set of smaller gaps:

1. **Router failover does not cover the whole transport failure surface.**
   `Router._attempt` (`src/llmbroker/broker/router.py`) catches only
   `httpx.HTTPStatusError`, `httpx.TimeoutException`, `httpx.ConnectError`, and
   `OSError`. httpx exceptions do not subclass `OSError`, so
   `httpx.ReadError` / `httpx.WriteError` / `httpx.RemoteProtocolError` /
   `httpx.ProxyError` propagate raw to the caller. So do a
   `json.JSONDecodeError` from `resp.json()` and a `KeyError`/`IndexError` from
   `message_from_response` when a provider returns 200 with a non-standard body
   (e.g. `{"error": ...}` with no `choices`). This breaks mission #1 ("the
   caller only sees an error once the whole pool is exhausted") and — worse —
   leaks the acquired slot: `in_flight` was incremented in `LLMPool.acquire`
   and no path decrements it, so with `parallel` set the model permanently
   loses capacity.
2. Quality scores are not validated; the Wilson bound assumes `[0, 1]`.
3. `run_tool_loop`/`arun_tool_loop` return `""` when `max_steps` is exhausted —
   silence, which the error contract forbids.
4. `AsyncBroker.calls()` is the only public method that skips `ensure_pool()`.
5. The documented quickstart writes a `.env` file that nothing in the package
   reads (`Secrets` resolves from `os.environ` only).
6. `llmbroker env` requires a local file; onboarding takes two commands where
   one would do.
7. `chat.py.__all__` declares transport internals public; CLAUDE.md still
   references a redis extra and fakeredis that do not exist.
8. Integration-test gaps: degraded-transport behavior, cluster cooldown on
   postgres/mongodb, sync-`Broker` thread concurrency, the CLI `sync`
   round-trip, and one no-mock real-socket test.

## Design decisions

Interpretation points this plan commits to; do not re-decide during
implementation.

1. **Transport failures cool down and fail over, like a 5xx.** The router
   catches `httpx.TransportError` (which covers connect/read/write/protocol/
   proxy/timeout errors) plus `OSError`, journals `CallStatus.ERROR` with
   `error_detail=type(exc).__name__`, applies the flat-base cooldown, and moves
   to the next model. No per-subclass distinctions.
2. **A malformed 200 response is a provider-side failure.** `call_provider`
   wraps body-shape errors (JSON decode failure, missing
   `choices[0].message`) in a new `InvalidProviderResponse(LLMRequestError)`
   (`src/llmbroker/exceptions.py`) carrying the model name and a truncated body
   snippet. The router treats it exactly like a 5xx: cooldown, journal,
   fail over. Rationale: a free-tier endpoint that answers 200 with garbage is
   misbehaving no less than one answering 503.
3. **The slot must be disposed on every path, including bugs.** `_attempt`
   gains a last-resort `except Exception` that releases the slot, journals the
   attempt, and re-raises — an unexpected exception is still surfaced (it is a
   bug to fix), but it can no longer leak `in_flight`. Every handled path
   already disposes via `release`/`cool_down`; a test asserts `in_flight == 0`
   after each failure mode.
4. **Scores are validated at the public entry points.**
   `AsyncResult.record_quality` and `AsyncBroker.record_quality` raise
   `ValueError` unless `0.0 <= score <= 1.0`. The sync wrappers inherit the
   check by delegation. Store backends stay dumb.
5. **Tool-loop exhaustion raises `ToolLoopLimitError(LLMRequestError)`**,
   message naming `max_steps`. Callers that want the old lenient behavior catch
   it; silence is not an option per the error contract.
6. **`calls()` calls `ensure_pool()`** like every other public method;
   architecture.md's method list is updated accordingly.
7. **`.env` support is explicit, dependency-free, and file-source-scoped.**
   `standalone.Secrets` gains an optional `env_file: str | Path | None`
   parameter: when set, the file is parsed lazily (stdlib only — `KEY=VALUE`
   lines, `#` comments, no interpolation) and consulted as a fallback after
   `os.environ` (real environment always wins). Source dispatch for a
   `.toml`/`.json` registry (`broker/source.py`) points `env_file` at the
   config file's sibling `.env` — a missing file is simply an empty fallback.
   DB sources and explicit `secrets=` objects are unaffected. This makes the
   README quickstart (`llmbroker env llms.toml > .env`) actually work.
8. **`llmbroker env` accepts a preset name as well as a path.** If the
   argument is not an existing file and matches the preset-name regex, fetch it
   the same way `preset` does and emit the skeleton from the downloaded TOML.
   Onboarding becomes `llmbroker preset freetier > llms.toml && llmbroker env
   freetier > .env` or even a single `llmbroker env freetier > .env`.
9. **`chat.py.__all__` shrinks to `["arun_tool_loop", "run_tool_loop"]`.** The
   transport helpers remain importable for the router but stop being declared
   public surface.
10. **No behavior knob for the HTTP timeout in this plan.** A per-LLM `timeout`
    (following the `parallel` metadata pattern) is deliberately deferred — it
    adds config surface without a concrete demand; revisit when a provider
    actually needs it.

## Phase 1 — router failover hardening (the correctness fix)

Files: `src/llmbroker/broker/router.py`, `src/llmbroker/chat.py`,
`src/llmbroker/exceptions.py`, `specs/reference/architecture.md` (error
contract paragraph), `specs/reference/optimizer.md` (cooldown section: transport
and malformed-response failures use the flat base).

1. Add `InvalidProviderResponse` to `exceptions.py`; raise it from
   `call_provider` around `resp.json()` and `message_from_response`.
2. Replace the `except (httpx.TimeoutException, httpx.ConnectError, OSError)`
   clause with `except (httpx.TransportError, OSError)`; add an
   `except InvalidProviderResponse` clause with the same cooldown treatment
   (journal `error_detail` from the exception, not just the type name).
3. Add the last-resort `except Exception` release-journal-reraise guard.
4. Tests (new `tests/test_router_degraded_transport.py`, MockTransport or
   patched `call_provider` raising the real exception types):
   - `httpx.ReadError` / `httpx.RemoteProtocolError` on model A → answer from
     model B; journal shows an ERROR row for A; A is COOLING.
   - 200 with invalid JSON, and 200 with `{"error": ...}` (no `choices`) →
     same failover; `error_detail` carries the snippet.
   - Single-model pool + transport failure + `wait=0` →
     `NoLLMAvailableError`, not a raw httpx error.
   - After every failure mode above: `broker._pool._slots[name].in_flight == 0`.
   - A deliberately planted unexpected exception (patched `call_provider`
     raising `RuntimeError`) propagates to the caller *and* leaves
     `in_flight == 0`.

## Phase 2 — small functioning fixes

Files: `src/llmbroker/broker/result.py`, `src/llmbroker/broker/broker.py`,
`src/llmbroker/chat.py`, `src/llmbroker/exceptions.py`,
`specs/reference/architecture.md`.

1. Score validation per decision 4; tests for both entry points (async + sync)
   covering `-0.1`, `1.1`, and the boundaries `0.0`/`1.0`.
2. `ToolLoopLimitError` per decision 5; test: dispatch that always returns
   tool calls exhausts `max_steps=2` and raises, message names the limit.
3. `ensure_pool()` in `AsyncBroker.calls()`; test: `calls()` on a fresh broker
   over a seeded registry does not raise and returns rows after one `ask`.

## Phase 3 — onboarding UX

Files: `src/llmbroker/standalone/secrets.py`, `src/llmbroker/broker/source.py`,
`src/llmbroker/cli.py`, `README.md`, docs quickstart,
`specs/reference/architecture.md` (backends table: default secrets note).

1. `Secrets(env_file=...)` per decision 7. Tests: env var wins over file;
   file-only ref resolves; missing file behaves as before; malformed lines are
   skipped, not fatal.
2. Source dispatch wires the sibling `.env` for `.toml`/`.json` sources; test:
   quickstart-shaped tree (`llms.toml` + `.env`, no exported vars) resolves
   keys through `AsyncBroker("llms.toml")`.
3. `llmbroker env <preset-name>` per decision 8 (share the fetch helper with
   `_cmd_preset`); tests: file path still works, preset name fetches (HTTP
   mocked), a name that is neither file nor valid preset errors clearly.

## Phase 4 — surface and doc hygiene

Files: `src/llmbroker/chat.py`, `src/llmbroker/broker/broker.py`, `CLAUDE.md`.

1. Trim `chat.__all__` per decision 9.
2. Remove the redis/fakeredis references from CLAUDE.md's dependency section
   (no redis extra or backend exists).
3. `_require_queryable`: check and return the same conceptual object — keep the
   `_base_store` capability check but drop the redundant `cast` gymnastics in
   favor of one clearly-commented line.

## Phase 5 — integration-test debt

New/changed test files only; no production code expected.

1. **Cluster cooldown on real DBs**: parametrize the
   `tests/test_cluster_cooldown.py` scenarios over the existing `stack`
   fixture (sqlite/postgres/mongodb) instead of sqlite-only.
2. **Real-socket end-to-end** (`tests/test_e2e_socket.py`): an in-process
   `asyncio` HTTP server (stdlib or a thin handler on `asyncio.start_server`)
   speaking OpenAI-compatible JSON, no patching anywhere: model A answers 429
   with `Retry-After: 1`, model B answers OK → first call served by B; after
   ~1s A is selected again (curated order). Marked as a normal test — it must
   run everywhere, no skips (per CLAUDE.md).
3. **Sync `Broker` concurrency** (`tests/test_sync_concurrency.py`): N threads
   × `ask()` against a `parallel=1` model with a mocked transport that asserts
   non-overlap; `close()` afterwards joins the loop thread within its timeout.
4. **CLI round-trip** (`tests/test_cli_sync_roundtrip.py`):
   `main(["sync", preset.toml, db.sqlite])` then `AsyncBroker("db.sqlite")`
   answers with a mocked transport — the documented DB-init workflow end to
   end.

## Out of scope

- LLM-as-judge (mission #2 autonomy) — already planned in
  [`llm-judge.md`](llm-judge.md), lands after Phase 1.
- Per-LLM HTTP timeout (decision 10).
- Any storage-schema change: nothing here touches table shapes.
