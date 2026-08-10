# A key is asked for once, and again only when something says so

Written against `one-broker-many-callers`, which takes the resolved key out of
the pool slot and gives it to a ring the broker holds per scope. This plan
decides what that ring asks the secrets port, and when.

## Goal

**A key that works is read once and reused for a day. A key that appears is used
by the very call that finds it.** Between those two, nothing polls: no timer over
a store that answers for free, and no per-request round trip to a store that
bills for it.

**The two questions are not the same question.** *Does a ref exist* is asked
constantly — every request of every user whose own key is absent, which is most
of them — and is answerable for a whole prefix in one call. *What is the value*
is asked rarely and only for refs that exist. Asking the second where the first
was meant is what makes a metered secrets backend expensive.

## What is broken

With the ring per broker and callers per request, a scoped caller resolves its
own ref on every request. On `standalone` and the DB backends that is a local
read. On `aws.Secrets` and `vault.Secrets` it is a billed network round trip in
front of every call, and for the ordinary user — the one who never set a personal
key — every one of those round trips returns "not found".

The same holds for the installation's own refs where a provider was never keyed:
that is a permanent state, not a transitional one, so anything that polls per ref
polls forever.

## Why

Two entries, to land in `decisions.md` verbatim. This plan argues nothing else
about them.

### key-freshness-is-two-questions

Whether a ref exists is answered from a listing of the prefix it lives under,
refreshed once per window of the asker's own activity — the caller's scope prefix
for personal refs, the root prefix for the installation's own. What a ref
resolves to is read once and held for a day, and dropped at once when a call
proves it dead.

**Blocks:** a TTL over the resolved value alone; caching absence per ref with a
window of its own; a knob on the broker or the backend for how stale a key may
be; a full listing of every ref an installation holds, refreshed on a clock.
**Why:** the two questions have opposite shapes. Absence is asked at request
rate and is the common answer, so it must cost one call for a whole prefix and
must not be asked per ref; a listing of a user's own prefix answers for all their
refs at once and is the same one call whether they have three keys or none. A
value is asked once per ref and changes only when a human changes it, so a day
bounds how long a process may spend a key the store no longer holds, and evidence
closes the case that matters — a key that stopped working says so on the first
call that spends it. Listing everything instead would fetch, for ten thousand
stored keys, a hundred pages a minute of which one is wanted today, which is the
cost of asking about users who are not here.
**Accepted cost:** a value replaced in the store while the old one still works is
picked up within a day rather than at once; a ref that appears is picked up within
one window of the asker's activity; and a backend that cannot list its refs gets
no absence answer at all and is asked every time, which is why only backends whose
lookup is free may go without listing.

### a-key-is-an-input-not-evidence

Everything a call needs to know about keys is settled before a model is chosen,
not on the observer that runs after the call.

**Blocks:** refreshing keys on the journal rebuild; picking a model first and
resolving its key second; letting a key read raise into the caller's request.
**Why:** the journal rebuild is evidence *about* calls and can wait for the next
one, which is why it rides the observer
([`propagation-rides-the-call-funnel`](#propagation-rides-the-call-funnel)). A
key is not evidence, it is what the call is made with: deferred to the observer
it would be picked up one call late, so the request that arrives right after the
key was stored — the one a human is watching — would be the one to fail. The
reason the same argument does not re-open the observer's own placement is that a
key read cannot fail the request: it is caught, the ring keeps what it holds, and
the call goes out on that.
**Accepted cost:** one listing sits in front of the first call of each window,
and a caller whose window has not elapsed waits it out — the pickup is bounded by
the window, not immediate.

## Work order

Three batches. `. ./activate.sh` first; `invoke pre` and `python -m pytest` green
after each.

### 1. Listing, in the port and in the backends

- `protocols/secrets.py` — an optional protocol for a backend that can enumerate:
  given a prefix, the refs it holds under it. A backend that does not implement it
  is asked ref by ref, as today.
- `aws/secrets.py` — ListSecrets filtered by the instance's own prefix plus the
  asked one, following pagination.
- `vault/secrets.py` — LIST under the mount path. **The ref must become one path
  segment**: a scoped ref is `scope/REF` today, so `llmbroker/scope/REF` makes
  LIST return directory names and never the refs themselves. Flatten the
  separator when building the path, and read it back the same way. No published
  users, so this is a change, not a migration.
- The DB-backed secrets (`backends/ports.py`) — one prefix query.
- `standalone` env secrets stay as they are: the lookup is free, so absence is
  answered by asking.

### 2. The ring decides when to ask

- `broker/keyring.py` — the ring gains the two windows: a held value carries when
  it was read and is re-read past a day; existence for a prefix carries when it
  was listed and is re-listed past the rebuild window. Both are checked at the
  moment the caller resolves, before selection.
- Absence is *not* stored per ref. A ref outside the last listing resolves to
  nothing without a call; a ref inside it that has no held value is read.
- `forget(ref)` on a dead key, from where the attempt failed, stays as
  `one-broker-many-callers` leaves it.
- Nothing raised by a listing or a read reaches the caller: it is logged once per
  ref and the ring answers from what it holds — the same rule the registry resync
  already follows.
- No constructor argument anywhere. The day and the window are the library's, and
  neither is a knob.

### 3. Docs and specs

- `docs/src/en/secrets.md` and the Russian copy — when a key you add starts
  working (the next call after the window, and why that is a minute and not a
  restart), when a rotated value is picked up, and what a backend that cannot
  list costs. `server.md`'s per-user section gets one sentence: a personal key
  stored on one node is live on the others within a window of their own activity.
- The specs named below, in this batch.

## Tests

`tests/test_keyring.py`, new, over a counting fake secrets backend:

- A held value is read once across many resolutions; past the day it is read
  again.
- A ref absent from the listing costs no read at all, however many callers ask.
- N callers of one scope inside a window produce exactly one listing; past it,
  two.
- A ref that appears in the store is used by the **first** call after the window,
  not the one after it — the entry above, asserted.
- A listing that raises, and a read that raises, leave the ring answering from
  what it holds and do not reach the caller.
- A key dropped by a 401 is re-read on the next resolution, and a different value
  in the store is what the next call spends.
- A backend with no listing is asked per ref, every time.

`tests/test_secrets.py` — over the existing LocalStack and Vault containers: the
listing returns exactly the refs stored under the asked prefix, a scoped ref
survives the Vault path flattening in both directions, and a ref stored by one
instance is listed by another.

## Spec updates

- `decisions.md` — the two entries above, verbatim.
- `rules/backends.md`, the secrets part — the two questions and where each is
  answered, that a backend which cannot enumerate is asked every time, and the
  scope-prefixed ref being one segment wherever a path is built. Link both
  entries.
- `rules/pool-health.md`, "The measure is key presence, and it never lags behind
  the keys" — the measure now follows the last listing and the failing call that
  disproves a key, and lags a stored change by at most the day. One clause.
- No new invariant: the rule is local to how keys are read, and `backends.md` is
  where a task about a secrets backend lands.

## The queue

**Ships with `one-broker-many-callers`.** It is written against that plan's ring
and is what makes a caller built per request affordable on a metered backend —
releasing 24 alone would publish a per-request round trip.

Independent of everything else queued. It writes user-facing strings in the docs
only, in plan 10's wording.

## Gate

`invoke pre` clean and `python -m pytest` green. Docker up for the LocalStack and
Vault testcontainers.
