# Correlation design v3 — revised against shipped state (2026-07-09)

Supersedes v2. The premise changed: what v2 planned to BUILD as Phase-0
substrate largely shipped as a side effect of #14/#15/#17. Correlation is
now a *connect-and-broaden* effort, not a build.

## Substrate status (verified against main)

- Attach-to-active investigation: BUILT (`upsert_alert` promoted-attach) but
  keyed on same-signature only (`a.signature = :s`, triage.py:178).
- Reopen for all FP closes: DONE (#15).
- Idempotency: DONE (#17 alert_source_events unique).
- Typed entities w/ role + provenance: DONE (#17 `entities` {type,value,role,source_field}).
- Occurrence vs observation time: DONE (#17).
- Concurrency safety (advisory lock, run-state matrix, active_run_for_case guard): DONE (#14).
- MITRE / rule_groups / decoder / template_hash on the wire + evidence store: DONE (#17).
- Structured verdicts (indexable): DONE (#3).
- Correlation-keys table: REFRAMED to a derived index over entities/iocs (not a source of truth).
- Settle window: MISSING.
- Multi-alert delivery to the graph: MISSING (claim is LIMIT 1, worker_runs.py) — this is the gap
  that means correlation currently groups investigations but NOT reasoning, so the cost win is unrealized.

## Sequenced plan (REVISED after adversarial review)

Correction: v3's original ordering put multi-alert delivery first as "the cost win."
Wrong — today's attach COALESCES into the existing promoted alert row (n_alerts stays 1);
it does not add alerts to an investigation. Multi-alert investigations don't exist until
entity-overlap attach (the keystone) inserts-and-links new alert rows. Riskiest assumption
corrected: the shipped same-signature attach sink does NOT generalize to correlation attach.

1. **Settle window (#28)** — independent, low-risk, first. not_before on investigation_runs +
   claim predicate (Alertmanager group_wait); severity>=12 bypass. Follow-up-run half
   (group_interval): a live run does NOT see later attaches (state snapshot before ainvoke,
   main.py:427) — attached alerts set new_evidence, a follow-up run is queued at terminal.
   Fixes the incorrect triage.py:677 comment.
2. **Entity-overlap attach (#27, KEYSTONE) + multi-alert delivery (#26)** together. #27 is bigger
   than v3 admitted — split into: (a) projected entity index table `alert_entity_keys` (JSONB
   entities has no queryable index), (b) rarity/hub-key stats (bucketed, not JSONB-at-ingest),
   (c) insert-and-link attach semantics (NOT merge-into-prior-alert) with explicit precedence vs
   closed-FP reopen, (d) group cap; entities stay out of alert_signature/_reopen_fields. #26 then
   delivers all alerts to one run with #11 compaction (verdict render is currently UNCAPPED,
   verdict.py:191; HIL alert_count hardcoded to 1, review_events.py:153).
3. **Verdict memoization + template-novelty (#29).** Dedicated tenant-scoped verdict-cache table
   keyed on stable shape (source, decoder, template_hash, template_version) — NOT alert_signature
   (has a 5-min bucket). Prior high-conf-FP structured verdict (#3) reused without an LLM call;
   novel templates full-triaged, known-benign suppressed (auto Dispatch-snooze).
4. **Learned layer (#30, async review-only).** Real features via #17. BUT start the HIL
   merge/detach label actions during #27 so labels accumulate. Deterministic match stays the only
   auto-attach; scorer suggests until labeled precision earns enforcement. Spike with MANUALLY
   labeled pairs gates any production scorer. pgvector = new op surface.

## New correlation signals #17 unlocked (beyond v2's key matching)

- Entity-graph via roles (actor/target/src/dst): lateral movement (same actor, many targets) vs
  targeted host (many actors, one target) — directed, not just co-occurrence.
- Kill-chain via MITRE tactics: correlate ATT&CK progression on one entity (incident narrative).
- Template-novelty routing: spend effort on novel log shapes, suppress high-frequency known-benign.
- Verdict memoization: structured verdict as the reusable value keyed by signature/template.

## Prior art mapping (native, no dependency)
- Netflix Dispatch: entity-dedup-into-existing-case == the typed-entity attach (step 2);
  snooze filters == template-novelty suppression (step 4).
- Prometheus Alertmanager: group_wait == settle window (step 3); group_interval == follow-up-run policy.

## Risks carried forward
- Hub keys: rarity/frequency demotion per typed entity (cheap aggregate now).
- Over-grouping: per-key-type + per-tenant windows, group-size cap (both exist from #14/#11 hooks).
- Dual representation: asset_ids stays frozen for coalescing/reopen; correlation keys off entities.
  Deliberate future revisit of coalescing onto typed entities (do not let the sidecar become permanent).
- Learned-layer feedback loop: labels from analyst actions only, never the scorer's own accepts.
- alerts JSONB unbounded growth (source_event_ids/initial_iocs) — pre-existing; aggregate in this program.

## Minimal Fix
To address the issue of missing checksums for OS packages, we need to generate checksums for the `.deb` and `.rpm` files in the `build-packer-images.yml` file. We can add the following lines to the `build-packer-images.yml` file, after the `*.xz` and `*.ova` files:

```yml
- "*.deb"
- "*.rpm"
```

This will ensure that the `SHA256SUMS.txt` file includes the checksums for the OS packages.