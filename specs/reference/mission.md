# llmbroker: mission and requirements

Zero-administration routing over a pool of 4-5 free-tier LLMs:

1. **Routing with failover**: don't hammer 429/503 — back off (trusting
   `Retry-After`) and move on to the next model within the same request. The
   caller only sees an error once the whole pool is exhausted.
2. **Zero administration**: a curated preset (TOML from the repository) that
   keeps itself current inside a running installation, with no admin act at all
   and never shrinking what the pool can call; a dead key is detected and
   disables itself; a model that performs poorly for our tasks moves itself to
   the back of the queue. The one irreducible admin act is obtaining a new
   provider key — and it is surfaced in the sync report, never silently absorbed.
3. **Learning per (model, operation)**: tasks require different levels of
   model capability — quality scores (`record_quality`) accumulate per
   (model, operation) pair, demotion is per operation; no global verdict
   exists.
4. **Keys optionally per-user, editable via the DB** (admin panel / personal
   account), with fallback to the shared key; the model list and learning
   are always shared; quota follows the key (429 and dead-key detection are
   scoped to the key actually used, 5xx is shared).
5. **Visibility from the host UI**: raw per-model facts (admin verdict, key
   presence, cooldown, per-operation demotions), a call journal, metrics —
   and the pool itself as a first-class object: how many providers can serve a
   request, which keys are missing, and whether the pool has degraded to a
   single quota. One call answers all of it; the UI chooses the presentation.
6. **One-liner and cluster**: for a script, a sync wrapper plus env keys and
   nothing else — `Broker()` takes no configuration and no file; the
   cluster's/stateless-server's shared cooldown is derived from the store
   journal.
7. **Batteries**: sqlite, postgres, mongodb (registry + store + secrets),
   aws/vault (secrets). A new backend is one driver file, ~200 lines.
8. **Cheap at low usage**: a bare broker makes zero DB calls; parallel calls
   to the same LLM are allowed by default (`parallel` restricts this for
   finicky providers).

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
   direct keys — strictly more free capacity than any one gateway account —
   and no third party ever sees the traffic. llmbroker must never require a
   dedicated running service of its own.
2. **No heavy dependencies.** The closest library, LiteLLM, is a large,
   fast-churning dependency surface whose cluster features push toward
   running its proxy server. llmbroker's core has zero mandatory
   dependencies; cluster-shared state derives from a journal in a database
   the host already runs (sqlite/postgres/mongodb as optional extras).
3. **Learned quality per (model, operation).** Existing routers balance on
   health, latency, or cost. Only llmbroker demotes a model per task kind
   from accumulated `record_quality` scores — a self-regulating pool, not a
   static priority list.
4. **Zero administration as a feature, not a tutorial.** A curated free-tier
   preset that keeps itself current, dead keys that disable themselves,
   cooldowns that honor `Retry-After` — competitors leave all of this as
   configuration for the operator.
5. **Per-user keys over one shared, learned pool.** Multi-user hosts get
   per-scope keys with shared-key fallback, while the model list and
   learning stay shared — a mode hosted gateways price as an enterprise
   feature and libraries do not model at all.

The decisions taken to satisfy these requirements and their cost estimate are
recorded in [`decisions.md`](decisions.md); the current behavior rules live in
[`architecture.md`](architecture.md) and [`optimizer.md`](optimizer.md).
