# llmbroker: mission and requirements

Zero-administration routing over a pool of free-tier LLMs from several
independent providers.

## The requirements

1. **Routing with failover**: a rate-limited or failing provider is backed off
   and the next one tried within the same request; the caller sees an error only
   once the whole pool is exhausted.
2. **Zero administration**: the curated model list keeps itself current inside a
   running installation, and obtaining a provider key is the one irreducible
   admin act — a free tier is issued to a person, not to a library.
3. **Learning per task kind**: what counts as a good enough model depends on
   what is being asked of it, so quality is tracked per (model, operation) and
   no global verdict exists.
4. **A paid model reached by name**, out of the same curated knowledge: the
   application names a permanent alias and llmbroker is what keeps it pointed at
   the current version, so no application and no human tracks a version bump,
   and no second package is installed to make a paid call.
5. **Keys optionally per-user**, with fallback to a shared key, over one shared
   model list. Quota follows the key, and an installation holding only per-user
   keys is a working installation rather than an empty one.
6. **Visibility from the host UI**: every per-model fact, the call journal, and
   the pool itself as a first-class object — how many providers can serve a
   request, which keys are missing, whether failover is still possible.
7. **A one-liner from a script**: env keys, a synchronous wrapper, no database,
   and nothing to administer.
8. **Several processes over one installation**: they share its configuration,
   they do not coordinate, and none of them may leave another's state wrong.
9. **Batteries**: sqlite, postgres, mongodb (registry + store + secrets),
   aws/vault (secrets). A new backend is one driver file.
10. **Cheap at low usage**: a bare broker needs no database at all, and parallel
    calls to one model are allowed by default and capped only where a provider
    demands it.

## The size of the problem

A pool is **a handful of endpoints** — a few independent providers, a model or
two each — served by **a few processes**, for at most **dozens of end users**.
That is not an observation about today's installations, it is the shape of the
problem: a free tier is quota that runs out, which is not the substrate anything
large is built on.

Three rules follow, and they outrank a local argument for precision:

- **Coarse wherever coarse is cheap and harmless.** Re-read everything on a slow
  clock rather than propagate a change exactly. Drop a cache rather than
  reconcile it. Replace state wholesale rather than merge it incrementally.
- **Exact only where an error is destructive**: losing what the installation
  stated itself, spending or leaking the wrong key, destroying a working
  configuration, telling a host the wrong reason a call failed, leaking a
  concurrency slot.
- **A mechanism justified by a scale this pool cannot reach is a defect, not
  headroom** ([`decisions.md`](decisions.md#size-is-part-of-the-mission)).
  Failover is what makes coarseness safe: the price of a stale fact is one
  wasted call, paid once by one process, and the caller never sees it.

## What it is not

The boundaries are as load-bearing as the requirements: each is a decision, not
a gap waiting to be filled, and a proposal that crosses one is arguing with the
mission rather than extending it.

- **Not a cluster runtime.** No coordinator, no leader, no lock, and no attempt
  to give several processes one exact view of the moment. What they share is the
  installation's configuration and its journal, and correctness never depends on
  how fresh one process's picture of another is.
- **Only OpenAI-compatible endpoints are pooled.** Breadth of provider adapters
  is the neighbouring product's offer, not ours; ours is that the endpoints in
  one pool are interchangeable enough to fail over between mid-request, which a
  translation layer per provider would not make more true.
- **Cost is not an axis.** Nothing is routed by price, and no tokens or spend
  are counted. The free pool has no prices to compare, and a host reaching its
  own paid model by name already chose what it is paying for.
- **Nothing wraps what is asked.** No prompt templates, no embeddings, no
  retrieval, no opinion about the content of a request or a reply. The tool loop
  is the one thing above a single call that ships, and all it does is repeat
  `chat` and hand each requested tool to the application's own dispatch.
- **Free tiers are the provider's terms, not ours.** The keys are the caller's
  own, and so is the agreement each provider issued them under; llmbroker
  neither multiplies a quota nor conceals whose it is.

## Positioning: what keeps llmbroker unique

Multi-provider failover routing by itself is a commodity — LiteLLM's router,
LangChain's `with_fallbacks`, or a hand-rolled retry loop all offer some form of
it. llmbroker's identity is the *combination* below; no existing solution covers
it, and a change that erodes any one of these erodes the mission:

1. **A library over the caller's own keys — never a hosted middleman.** Hosted
   gateways (OpenRouter, Portkey, Cloudflare AI Gateway, Helicone) proxy traffic
   through their servers under their account: their billing, their data path,
   one shared rate-limit bucket, and a single point of failure. llmbroker pools
   the providers' *own* free tiers via direct keys, so a pool holding keys for
   several providers draws on that many independent quotas where a gateway
   account draws on one, and no third party sees the traffic. Keys for one
   provider are not a pool — spilling from one provider onto another is the whole
   mechanism. What llmbroker centralizes is curation, on the configuration path
   and never on the data path: it publishes text on GitHub, and an installation
   that cannot reach it keeps running on what it already holds. It must never
   require a running service of its own.
2. **Nothing large comes with it.** The closest library, LiteLLM, is a large,
   fast-churning dependency surface whose cluster features push toward running
   its proxy server. llmbroker's core is a handful of small pure-Python packages
   — no provider SDK, no database driver, no framework — so embedding it does not
   measurably grow the application that does. Every backend is an optional extra.
3. **Learned quality per (model, operation).** Existing routers balance on
   health, latency, or cost. llmbroker is the only one that accepts a quality
   signal from the host and turns it into the pool's order, per task kind — a
   pool that regulates itself on the host's own definition of a good answer,
   rather than a static priority list.
4. **Zero administration as a feature, not a tutorial.** A curated free-tier
   preset that keeps itself current, dead keys that disable themselves, cooldowns
   that honor `Retry-After` — competitors leave all of this as configuration for
   the operator.

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

**Every process learns for itself.** What processes share is the installation's
configuration and its journal; what a process finds out about availability —
cooldowns, dead keys — lives in its own memory and is sent nowhere. Correctness
does not depend on how fresh that picture is: a stale fact costs one wasted call,
and failover absorbs it.

**Learning never writes configuration.** What the host rates and what the pool
observes change how models are ordered and nothing else; the stored model list
changes only when a sync merges one.

**The routed pool is whatever the registry holds.** Naming a model to call it is
a different act from putting an endpoint in the pool: a model reached by name is
never routed onto, never failed over from, and never learned about as a pool
member. It is stated where the application that calls it is configured — a
version the host pins and owns, or a permanent alias llmbroker keeps pointing at
the current version, so application code survives a version bump and learns
about it from a log line.

**The model list is never frozen.** Free endpoints are retired without notice,
so a pinned list decays into nothing. Keeping it current is llmbroker's own job
wherever it is allowed to be: a deployment forbidden to open any connection but
to its providers takes that job over, and none may decline it. The refresh rides
on activity rather than on a timer of its own, and it never rewrites what the
installation stated itself. A curated preset is the only shape a list arrives in;
an installation that must not follow ours states its whole pool itself, and gets
no second configuration form for us to keep in step.

**One answer costs one request, unless someone decides otherwise.** The quota
being pooled is the scarce thing, so a healthy call goes to one model and waits.
Two things spend more, and both are deliberate: a caller may buy latency with
quota by asking for the fastest of several models, and the pool covers its own
recovery, so the first call made after a withdrawn model's pause has elapsed is
not left to gamble the caller's latency on it alone. A caller that would rather
keep the request can decline that cover.

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

**llmbroker serves facts and never a verdict on them.** What the host can see is
raw: which models are cooling, which keys are missing, how many providers can
still answer. There is no severity scale, no status enum and no alert stream,
because how bad a fact is depends on the installation — two providers is a
crisis for one host and the intended shape for another. The few genuinely
human-actionable events are log lines; everything else the UI reads and judges
for itself.

**Errors are a contract, not prose.** One exception carries a machine-readable
reason for "nothing could answer"; every other failure state a host must handle
has its own type, and lifecycle failures sit on their own branch. A host never
has to match on message text.

---

Everything below this is detail: [`invariants.md`](invariants.md) holds the
cross-cutting rules and indexes the rest.
