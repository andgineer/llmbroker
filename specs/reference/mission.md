# llmbroker: mission and requirements

Zero-administration routing over a pool of free-tier LLMs from several
independent providers:

1. **Routing with failover**: don't hammer 429/503 — back off (trusting
   `Retry-After`) and move on to the next model within the same request. The
   caller only sees an error once the whole pool is exhausted.
2. **Zero administration**: a curated preset (TOML from the repository) that
   keeps itself current inside a running installation, with no admin act beyond
   the keys themselves and never costing the installation a provider it could
   reach; a dead key is detected and disables itself; a model that performs
   poorly for our tasks moves itself to the back of the queue. Obtaining a
   provider key is the one irreducible admin act — a free tier is issued to a
   person, not to a library — so a missing key is always surfaced to whoever has
   to supply it, never silently absorbed.
3. **Learning per (model, operation)**: tasks require different levels of
   model capability — quality scores accumulate per (model, operation) pair,
   demotion is per operation; no global verdict exists.
4. **Keys optionally per-user, editable via the DB** (admin panel / personal
   account), with fallback to the shared key; the model list and learning
   are always shared; quota follows the key (429 and dead-key detection are
   scoped to the key actually used, 5xx is shared).
5. **Visibility from the host UI**: raw per-model facts (admin verdict, key
   presence, cooldown, per-operation demotions), a call journal, metrics —
   and the pool itself as a first-class object: how many providers can serve a
   request, which keys are missing, and whether the pool has degraded to a
   single quota. llmbroker serves the facts and never a verdict on them — the
   UI chooses the presentation.
6. **One-liner and cluster**: for a script, a sync wrapper plus env keys and
   nothing else — a bare `Broker()` takes no configuration and leaves no file
   to maintain; the cluster's/stateless-server's shared cooldown is derived
   from the store journal.
7. **Batteries**: sqlite, postgres, mongodb (registry + store + secrets),
   aws/vault (secrets). A new backend is one driver file.
8. **Cheap at low usage**: a bare broker makes zero DB calls; parallel calls
   to the same LLM are allowed by default, restricted per model for finicky
   providers.

## Positioning: what keeps llmbroker unique

Multi-provider failover routing by itself is a commodity — LiteLLM's router,
LangChain's `with_fallbacks`, or a hand-rolled retry loop all offer some form
of it. llmbroker's identity is the *combination* below; no existing solution
covers it, and a change that erodes any one of these erodes the mission:

1. **A library over the caller's own keys — never a hosted middleman.**
   Hosted gateways (OpenRouter, Portkey, Cloudflare AI Gateway, Helicone)
   proxy traffic through their servers under their account: a single point
   of failure, their billing/markup, their data path, and one shared
   rate-limit bucket. llmbroker pools the providers' *own* free tiers via
   direct keys, so a pool holding keys for several providers draws on that
   many independent quotas where a gateway account draws on one — and no
   third party ever sees the traffic. (Keys for one provider are not a pool:
   spilling from one provider onto another is the entire mechanism.)
   llmbroker must never require a dedicated running service of its own.

   It does centralize one thing — deciding what is worth pooling — but on the
   configuration path rather than the data path, and that difference is what
   the design defends. What it publishes is a text file on GitHub. Unreachable,
   and nothing happens: an installation keeps running on the lineup it already
   holds. Wrong, and it still cannot destroy a working configuration: an
   arriving lineup is merged under rules that never cost an installation a
   provider it could reach. There is no service to be denied by and no runtime
   dependency to fail.
2. **Nothing large comes with it.** The closest library, LiteLLM, is a large,
   fast-churning dependency surface whose cluster features push toward
   running its proxy server. llmbroker's core is a handful of small
   pure-Python packages — no provider SDK, no database driver, no framework —
   so embedding it does not measurably grow the application that does.
   Every backend is an optional extra, and cluster-shared state derives from a
   journal in a database the host already runs.
3. **Learned quality per (model, operation).** Existing routers balance on
   health, latency, or cost. llmbroker is the only one that accepts a quality
   signal from the host and turns it into the pool's order, per task kind — a
   pool that regulates itself on the host's own definition of a good answer,
   rather than a static priority list.
4. **Zero administration as a feature, not a tutorial.** A curated free-tier
   preset that keeps itself current, dead keys that disable themselves,
   cooldowns that honor `Retry-After` — competitors leave all of this as
   configuration for the operator.
5. **Per-user keys over one shared, learned pool.** Multi-user hosts get
   per-scope keys with shared-key fallback, while the model list and
   learning stay shared — a mode hosted gateways price as an enterprise
   feature and libraries do not model at all.

## The design this produced

The load-bearing choices, for a reader who will not go through the rule files:
enough to predict how the library behaves and to tell a change that fits from
one that fights the design. Each is a decision, not a mechanism — how any of it
is carried out is the implementation's business.

**The provider is the unit, not the model.** Failover buys something only across
independent quotas, so two endpoints behind one key count once — in what the
pool is curated to hold, in whether it is healthy, and in what a refresh may
take away. A pool of many models on one key is not a pool.

**Three pluggable ports, only one required.** Where configurations live, how a
key reference becomes a key, and where calls are recorded. Zero-dependency
defaults ship for all three, so the library works with no backend at all, and
naming one source is enough to derive all three.

**Nothing learned is written down.** Everything llmbroker knows beyond the
stored lineup — cooldowns shared across nodes, quality windows, metrics — is
re-derived from an append-only record of calls. Learning writes to no
configuration, and the lineup changes only when a sync merges one. There is no
second state subsystem to reconcile, which is what lets a stateless cluster
share what it learned without a running service of its own.

**The routed pool is exactly the curated lineup.** Failover only works across
endpoints curated as interchangeable, so a host's own model is never routed
onto, never failed over from, and never learned about as a pool member.
Reaching it by name is a separate act, and paid models follow permanent aliases
so application code survives a version bump.

**The lineup keeps itself current, unconditionally.** Free endpoints are retired
without notice, so a pinned lineup decays into nothing. The refresh rides on
activity rather than on a timer of its own, and it may never cost the
installation a provider it could reach. An installation that must not follow our
curation states a lineup of its own instead of freezing ours.

**Availability and quality are separate axes.** Cooldown is provider-driven,
self-healing, and withdraws a model; quality demotion is host-driven, sticky,
and only reorders. Keeping them apart is what allows demotion to have no
timer — an excluded model returns by itself, a demoted one returns on evidence.

**Quality is the host's verdict, never llmbroker's own.** Only a rating the host
supplies enters the quality window: nothing inferred, nothing auto-generated,
and no model judging another's answer. A host that has no quality signal gets
curated ordering and loses nothing else; one that has it — did the JSON parse,
did the user accept the answer — spends nothing to use it. Scores are per task
kind, because a model good enough to classify may be too weak to summarize.

**Errors are a contract, not prose.** One exception carries a machine-readable
reason for "nothing could answer"; every other failure state a host must handle
has its own type, and lifecycle failures sit on their own branch. A host never
has to match on message text.

---

Everything below this is detail: [`invariants.md`](invariants.md) holds the
cross-cutting rules and indexes the rest.
