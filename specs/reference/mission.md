# llmbroker: mission and requirements

Zero-administration routing over a pool of 4-5 free-tier LLMs:

1. **Routing with failover**: don't hammer 429/503 — back off (trusting
   `Retry-After`) and move on to the next model within the same request. The
   caller only sees an error once the whole pool is exhausted.
2. **Zero administration**: a curated preset (TOML from the repository); a
   dead key is detected and disables itself; a model that performs poorly for
   our tasks moves itself to the back of the queue.
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
   the UI chooses the presentation.
6. **One-liner and cluster**: a sync wrapper + TOML + env keys for scripts;
   the cluster's/stateless-server's shared cooldown is derived from the
   knowledge journal.
7. **Batteries**: sqlite, postgres, mongodb (registry + knowledge + secrets),
   aws/vault (secrets). A new backend is one driver file, ~200 lines.
8. **Cheap at low usage**: a bare broker makes zero DB calls; parallel calls
   to the same LLM are allowed by default (`parallel` restricts this for
   finicky providers).

The plans implementing these requirements live in `specs/plans/simplify-core.md`
→ `specs/plans/simplify-learning.md` → `specs/plans/simplify-storage.md`. The
decisions taken to satisfy these requirements and their cost estimate are
recorded in `specs/plans/simplify-rationale.md`.
