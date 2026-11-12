# Plan — three things a host had to build around the library

**Status: source-bound on `7daec8c3c`.** Three independent items, each a workaround
read in a host's source on 2026-11-12. One batch each; no item depends on another.

**Queue position:** second, after [`release-pipeline.md`](release-pipeline.md) and before
[`architecture-simplification.md`](architecture-simplification.md). Item 1 changes what
`ask`/`chat` does after an expired budget, which that plan's "one divergence" paragraph
measures — its revalidation re-measures the paragraph against this handover.

## 1. A caller's budget holds across the pass that follows a re-read

**Now.** `AsyncLLMs.chat` (`broker/llms.py`) catches `NoLLMAvailableError`, lets the broker
re-read its ports (`AsyncBroker._on_exhausted`), and on a re-read that ran calls
`Router.chat` again with `wait=0`. `wait=0` means "do not queue", and leaves every attempt
on `chat.HTTP_TIMEOUT` (60 s). So a call made with `wait=3` whose budget expired — or whose
pool was exhausted early — may then spend up to 60 s per free model. The re-read runs for
any caller not holding every key the pool names, which is the ordinary state of an
installation with per-user keys, once per debounce period.

`rules/model-list.md` (rebuild trigger 4) already states the intent: "a caller's `wait` is
a promise about how long it may be held, and a second pass may not spend it again". The
mechanism keeps the queueing half of that promise and breaks the answering half.

The stream path is already right: `RoutedStream._acquire` (`broker/stream_owner.py`)
re-acquires under the handle's original deadline, and its attempts keep the original budget.

**Evidence.** lexiflux runs its 3-second inline translation on a daemon thread and abandons
it at the limit (`lexiflux/language/llm.py`, `ask_inline_translation`), with the comment
"`wait` does not bound the broker's second pool pass".

**Do.**
- In `AsyncLLMs.chat`, take the call's deadline once, after `_ensure_pool()` and before the
  first pass: `None` for `wait is None` or `wait == 0`, else `time.monotonic() + wait`.
- After a re-read that ran: with a deadline already passed, re-raise the first pass's error
  — the re-read still happened and serves the next call. Otherwise run the second pass
  without queueing, as today, with every attempt bounded by that deadline.
- `Router.chat` and `Router._route` take the deadline as a private keyword
  (`_answer_deadline: float | None = None`); when given, `_route` uses it for
  `answer_deadline` instead of deriving one from `wait`. `wait=0` keeps bounding the
  acquisition. No public signature changes.
- `wait=None` and `wait=0` callers keep today's second pass exactly: they asked for the
  global ceiling.

**Tests** (`tests/test_rebuild_triggers.py`, beside
`test_a_key_stored_while_the_pool_is_exhausted_answers_the_same_call`; `tests/test_wait_budget.py`
for the bound itself), each failing before the fix:
- a positive `wait`, one keyed model that hangs, a key landing during the call for a second
  model that also hangs: the call raises within `wait` plus a small margin, not after the
  ceiling; the second model's attempt was opened with a timeout no larger than what remained;
- a positive `wait` that expired on the first pass: the rebuild ran once, no second attempt
  was opened, and the error raised is the first pass's own (`reason="timeout"`);
- a positive `wait`, pool exhausted at once (`reason="no_keys"`), a key landing: the same
  call answers — the case the trigger exists for is intact;
- `wait=None` and `wait=0`: the second pass runs on the ceiling, unchanged;
- lower bounds on measured spans use `CLOCK_SLACK`.

**Specs.** `rules/model-list.md`, trigger 4: the pass queues for nothing *and* ends at the
caller's own deadline; a budget already spent leaves the error standing. `rules/call-path.md`,
the `wait` section, links to it rather than restating it. No `decisions.md` entry: this
makes the code keep a rule already written.

## 2. The synchronous broker re-reads its keys on request

**Now.** `AsyncBroker.rebuild()` is public; `Broker` (`sync.py`) passes through `sync`,
`snapshot`, `ensure_pool`, `disable_llm`, `enable_llm` and not `rebuild`.

**Evidence.** lexiflux subclasses `Broker` and calls `self._run(self._async.rebuild())`
(`lexiflux/language/broker.py`, `RebuildableBroker`) so a key a user has just saved takes
effect at once.

**Do.** Add `Broker.rebuild()` delegating like its neighbours.

**Tests** (`tests/test_sync.py`): a key added to a `DictSecrets` after the pool was
provisioned is not paid with until `broker.rebuild()`, and is after it.

**Specs.** Nothing: the four rebuild triggers are unchanged, and a host calling `rebuild`
is the same act on either facade. `docs/src/en/usage.md` names it where `sync` is described
for the synchronous broker.

## 3. The default secrets can say which names they hold

**Now.** `Secrets` (`standalone/secrets.py`) resolves one ref at a time and cannot list.
`rules/backends.md` records that as sufficient because an environment read is free and "an
installation that gives its users keys of their own keeps them in a store".

**That combination does arise.** A host keeping its users' keys in its own database and the
server's in the environment writes one secrets object over both, and must answer the listing
for both halves. Without a listing on `Secrets` it re-reads the environment and `.env`
itself, through the internal `parse_env_file`:
- lexiflux — `server_refs()` in `lexiflux/language/broker.py`, feeding its own `refs()`;
- dinary — `chat_key_available()` in `src/dinary_analytics/llm.py`, re-stating the blank-value
  rule in a comment so it stays in step.

**Do.** Give `Secrets` `refs(prefix="")`: every name with a non-blank value in the
environment or, behind it, the env file — exactly the names `resolve` would answer for.
It thereby satisfies `EnumerableSecretsProtocol`, so a broker over it takes one listing per
rebuild like any other enumerable store. `DictSecrets` gets the same method, so the two
zero-dependency resolvers behave alike.

**Tests** (`tests/test_env_file_secrets.py`, `tests/test_secrets.py`): a blank export and a
bare `KEY=` line are not listed; an environment value hides the file's; a file changed on
disk is re-read; the prefix filters; for every listed name `resolve` succeeds and for an
unlisted one it raises. `tests/test_rebuild_triggers.py`: a broker over `Secrets` enumerates
once per rebuild and resolves the same keys as before.

**Specs.** `rules/backends.md`, the listing paragraph: the environment-backed resolver lists
too, and the reason is composition, not read cost. Remove the sentence that the combination
does not arise; `rules/selection.md` keeps its statement for a store that cannot list.

## Out of scope

- A per-call list of the models a call may use. One host records wanting it and defers it
  itself until the extra wait is noticed in use.
- Any change to the stream path's re-read, or to when the re-read fires.

## Gate

`. ./activate.sh`, then `invoke pre` and `invoke test` after each item, and `invoke
downstream` once at the end. No version bump, no commit.

The handover names, per host, the workaround that can now be deleted: lexiflux's inline
translation thread, `RebuildableBroker` and `server_refs()`; dinary's own env-file read in
`chat_key_available()`. The runner edits no host.
