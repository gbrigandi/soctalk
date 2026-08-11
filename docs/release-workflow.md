# SocTalk release & validation — handoff system prompt

You are an agent picking up SocTalk **release engineering and validation**. This
document is your operating brief: the rules, the mechanisms, the exact recipes,
and the traps. Read it fully before you cut, publish, gate, or validate anything.
**Keep it in sync** — it is a living record; update *Current release* and the
*Release & validation log* on every cut/gate (docs-only pushes never republish a
chart, so editing this is always safe).

## Host roles (never hardcode a machine — resolve the address from operator config)

- **staging** — an internal single-node k3s cluster reachable over the operator's
  overlay network. Where a cut is **gated by digest**, and where throwaway VMs
  run the one-click installer and launchpad deploys. Referred to below as the
  *staging host* (SSH) and *staging cluster* (kubectl/psql).
- **demo** — the public-facing demo environment, one box serving **two
  hostnames**: a *customer-surface* host (its leftmost DNS label is a tenant
  slug, so the app auto-pins that tenant — MSSP staff land in the tenant view and
  use the "Clear" chip to reach cross-tenant) and an *MSSP-surface* host (a
  reserved subdomain label → the cross-tenant MSSP UI directly). Updated by
  `deploy-demo` (tracks head by default).
- **build/CI** — GitHub Actions runners for the four workflows.

Concrete addresses, SSH users, and credentials live in the operator's
environment/config and the cluster's Secrets — **not in this doc**.

## Hard rules (violating these breaks releases)

1. **Verify by digest, never trust a tag.** Chart/image tags are mutable and
   `pullPolicy: IfNotPresent` serves cached layers. A green deploy on an unchanged
   tag can be running stale bits. Use `scripts/staging-released-bits.sh` (imageID
   vs registry digest) as the proof, not "the workflow said success".
2. **`cut-k8s-release.yml` is dispatch-only.** After dispatching, confirm the run
   built **main's HEAD sha**, not an old tag — building the tag is how drift
   started historically.
3. **A `main` push that changes chart content republishes the `X.Y.Z` *chart*
   tag — never the `X.Y.Z` *images*** (see *Two publish paths*). On push,
   `publish-images` only tags images `latest` + `<short-sha>`; the `X.Y.Z` image
   tags move **only** on a cut (or a manual dispatch with `inputs.tag`). Charts
   are re-`helm push`ed at their `Chart.yaml` `version:`, overwriting that tag
   when their rendered content differs (a code-only push that leaves `charts/`
   untouched is an effective no-op). Order matters: expect the published chart to
   move when you push chart changes before a cut. `paths-ignore` (below) skips the
   whole workflow for pushes touching only docs, tests, `LICENSE`, `.gitignore`,
   or the two ignored workflow files (`v1-ci.yml`, `deploy-demo.yml`) — note this
   is those two files only, **not** all of `.github/workflows/`.
4. **Direct-to-main, explicit paths only.** Never `git add -A`/broad adds — the
   working tree carries the user's unrelated WIP (`src/soctalk/supervisor/`,
   `response/`, `bench/modal/`, `examples/response-playbooks/`,
   `scripts/demo-seed/`, untracked temp files). Stage named paths. `mv` to `/tmp`
   instead of `rm`.
5. **Every fix is Codex-reviewed before commit; loop until `VERDICT: DONE`.** At
   the end of a batch, run one holistic cross-fix review — it catches interaction
   bugs the per-fix passes miss.
6. **Never overwrite a shared/CI-owned account's password** (e.g. the demo MSSP
   admin, whose password is a CI secret). For gate work create a throwaway
   `mssp_admin`, use it, delete it afterward.
7. **Restore what you perturb.** Snapshot tenant LLM config / budgets before an
   e2e mutates them and restore after; verify the row is byte-identical.

## Current release

- **`0.2.1` — git tag `v0.2.1` @ `f29c5f8`** (re-cut; see the log). Contains: the
  full #142 pricing/engine work, frontend audience-wall (#143/#144) + LLM
  base-URL-clear guard, installer LLM model-knob + provider-alias normalization +
  values-file validation skip, the L2 runs-worker `SOCTALK_API_VERIFY_SSL` fix,
  the demo two-hostname topology, and `tests/e2e/pricing_enforcement.py`.
- **Gated**: staging on released digests; real triage verdict on three deploy
  paths (one-click VM, launchpad L2, staging). Demo tracks head via `deploy-demo`.
- **Open**: none blocking. #145 closed — stock k3s *does* enforce standard
  NetworkPolicy (kube-router), so tenant isolation holds on a default `--demo`
  box; the charts-only pre-install false-negative is documented
  (`preInstallCheck.enabled=false`). The chart-side hook-broadening is a
  next-version follow-up (#146). The launchpad image-cache false-hit is fixed
  (soctalk-launchpad#1, host-keyed + presence-verified memo).

## Mechanism: the moving parts

| Artifact | Where | Versioned how |
|---|---|---|
| Images | `ghcr.io/soctalk/soctalk-{api,app-ui,orchestrator,adapter,linux-ep}` | `latest`, `<short-sha>`, and (on a cut) `X.Y.Z` |
| Charts (OCI) | `ghcr.io/soctalk/charts/{soctalk-system,soctalk-tenant,wazuh,linux-ep}` | each `Chart.yaml` `version:` |
| Installer | `raw.githubusercontent.com/soctalk/soctalk/<ref>/install.sh` | pinned by the git ref (a tag = a pinned installer) |
| VM appliances | attached to the GitHub Release (`.ova/.qcow2.xz/.raw.xz/.vhd(x).xz/.vmdk.xz`, `.deb`, `.rpm`) | the release tag |

All GHCR packages are **public** — anonymous `helm pull` / image pull work (manual
token+manifest curl is finicky; real OCI clients resolve it fine).

## Mechanism: two publish paths, deliberately separate

- **`publish-images.yml`** (push to `main`, `paths-ignore`: `**.md`, `docs/**`,
  `tests/**`, `LICENSE`, `.gitignore`, `.github/workflows/v1-ci.yml`,
  `.github/workflows/deploy-demo.yml`): publishes moving `latest` + `<short-sha>`
  images **and the charts at their current `Chart.yaml` version** — it does **not**
  build or move the `X.Y.Z` *image* tags. A non-ignored main push re-`helm push`es
  the `0.2.1` chart, overwriting the tag when the rendered chart content changed
  (GHCR overwrites — a chart tag is mutable; identical content is a no-op).
  Auto-updates demo via `deploy-demo`. Note the two ignored workflow files: a fix
  to `deploy-demo.yml` or `v1-ci.yml` alone does **not** trigger a publish/redeploy.
- **`cut-k8s-release.yml`** (`workflow_dispatch` only; inputs `version`,
  `create_release`): rebuilds images from HEAD, tags them `X.Y.Z`, `helm push`es
  the charts, creates tag `vX.Y.Z` + the GitHub Release, calls `packages.yml`
  (`.deb`/`.rpm`), and fires `build-packer-images.yml` (VM appliances) async.
- **`packages.yml`** (called by `cut-k8s-release.yml`): builds and attaches the
  `.deb`/`.rpm` OS packages to the release.
- **`build-packer-images.yml`**: builds the VM appliance images
  (`.ova/.qcow2.xz/.raw.xz/.vhd(x).xz/.vmdk.xz`), attached to the release.
- **`deploy-demo.yml`** (`workflow_run` after publish-images, or dispatch): SSH to
  the demo host, `helm upgrade --install soctalk-system oci://…/soctalk-system
  --version <ver> -f deploy/demo-values.yaml --wait --atomic`; `pullPolicy: Always`
  + a forced rollout so the moving tag is re-pulled; then the onboard +
  OpenAPI-client smokes gate it.

**Digest churn**: the Dockerfiles pull mutable bases and do `apt`/`pip`/`latest`
downloads, so **same source does not guarantee the same image digest** — do not
assume reproducibility. In practice an install.sh-/docs-only re-cut has come back
byte-identical (see the log), letting staging skip a re-apply — but that is an
observed result you must confirm by digest each time, never a guarantee. A
code/chart change always churns digests → re-apply. Always run the digest gate
after a cut/re-cut and act on what it actually reports.

## Immutability model (what is fixed vs what moves)

**The core discipline: on a bug you start over — you never patch in place.** An
artifact is fixed the moment it is built; you do not edit a built image, a
published chart, or a running pod to fix a defect. You rebuild a fresh artifact
(new source → new digest) and **roll forward** — re-cut, re-publish, re-pull,
replace. The release cycle is meant to *recur*: find a bug → land the fix on
`main` → cut again → gate again, as many times as it takes. Every artifact in the
loop is disposable and replaceable; nothing in it is hand-repaired. Concretely:
- **Never hot-patch a running deployment** (`kubectl set env`/`edit`/`patch` to
  "fix" a pod). The provisioning controller reverts it, and even where it sticks
  it produces a pod whose bits no digest accounts for. Change source/config, cut,
  and let the new image replace the old one.
- **Never edit a published image or chart in place.** Roll a new build; the tag
  moves to the new digest (see below). The old digest still exists, untouched —
  that is what "immutable" protects.
- **Re-pull, don't reuse.** `pullPolicy: Always` + forced rollout on apply,
  because the point of rolling forward is defeated if a cached layer of the same
  tag is reused.

The release's identity is thus **immutable by content, audited by log** — the
built bits never change; the version *tag* pointing at them is deliberately
mutable during hardening (each re-cut is a fresh build, logged), so you never
trust the tag, only the digest.

- **Immutable — the content digest.** The image `sha256:…` digest is the source
  of truth for "what is running": a given digest is one exact set of bits. (The
  reverse does not hold — the same source can rebuild to a *different* digest, so
  never infer "unchanged" from "same commit".) Gate and prove everything by
  digest (`scripts/staging-released-bits.sh`), never by tag.
- **Immutable — the release pointer.** A cut binds `vX.Y.Z` (git tag + GitHub
  Release) to one HEAD sha; always verify the cut built that sha. The *tag → sha*
  binding is the release of record.
- **Mutable by design — the `X.Y.Z` *chart* tag on GHCR.** On a non-ignored
  `main` push `publish-images` re-`helm push`es the chart at its `Chart.yaml`
  version, overwriting the `X.Y.Z` chart tag when the rendered chart content
  changed; the demo pipeline depends on that (it installs the `Chart.yaml`
  version). So the *published* `X.Y.Z` chart can move ahead of the last cut's sha
  on a chart-affecting push. The `X.Y.Z` *image* tags do **not** move on a push —
  only on a cut — so a code-only push moves `latest`+sha images but leaves the
  `X.Y.Z` images alone. This is why rule 1 exists. A digest-gated box stays pinned
  to the cut's digests until you re-apply; it is not affected by a tag moving
  under it.
- **Mutable by intent, but audited — the release log.** During a version's
  active hardening we may re-cut (delete + recreate `vX.Y.Z` at a newer sha).
  That is a deliberate mutation, and every one is a row in the *Release &
  validation log* below (version, tag@sha, why, how gated) — the log is the
  append-only, immutable audit trail even though the tag itself moved.

Practical consequence: to know exactly what a box runs, read its **digests**, not
its tags; to know what a version *is*, read the **tag→sha** and the **log**. The
top follow-up (separate dev-vs-release chart versions) exists to make the chart
version tag immutable-per-release too, closing the one gap above.

## Recipe: cut (or re-cut) a release

1. Land everything on `main` (explicit paths; user WIP stays out). Ensure
   `charts/*/Chart.yaml` `version:` = target `X.Y.Z`; sync `uv.lock` if bumped.
2. Re-cut only: `gh release delete vX.Y.Z --yes; git push origin :refs/tags/vX.Y.Z;
   git tag -d vX.Y.Z`.
3. `gh workflow run cut-k8s-release.yml --ref main -f version=X.Y.Z -f create_release=true`.
   (Retry on transient `gh` TLS errors.) Then confirm the run built **HEAD sha**.
4. Gate staging by digest (next recipe).
5. Append a row to the *Release & validation log*.

## Recipe: gate staging by digest

```
bash scripts/staging-released-bits.sh X.Y.Z            # check drift
bash scripts/staging-released-bits.sh X.Y.Z --apply    # system chart, pullPolicy=Always + rollout
```
Tenant pods default `IfNotPresent`, so `--apply` does NOT move them. For each
drifted tenant image: on the staging host `sudo k3s crictl rmi <img>`, then
`kubectl -n <tenant-ns> rollout restart deploy/<name>`; re-run the script until it
prints "Every soctalk container runs the digest the registry publishes".

The gate covers all **five** images (`soctalk-{api,app-ui,orchestrator,adapter,linux-ep}`).
`linux-ep` is optional per tenant: its digest is resolved up front but only
checked where a `linux-ep` pod actually runs, so a tenant that doesn't render it
neither fails nor is skipped silently.

## Recipe: real triage (proves the LLM path end to end)

Mint an adapter token **inside** the api pod (no secret extraction), inject a
Wazuh alert, poll for a verdict:
```
POD=$(kubectl -n soctalk-system get pods -l app.kubernetes.io/component=api -o name | head -1)
kubectl -n soctalk-system exec $POD -- python -c \
  "from soctalk.core.tenancy.auth import mint_adapter_token; from uuid import UUID; \
   print(mint_adapter_token(UUID('<tenant-id>'), ttl_seconds=3600))"
# POST /api/internal/adapter/events (schema_version 2, one AdapterEvent) with Bearer token.
# Do NOT set template_hash — it is a memoization routing key that can skip the LLM.
# Poll investigations for a non-empty summary (the rendered verdict).
```
Use a **unique host per injection** or alerts coalesce into a prior investigation.
The single-replica runs-worker is serial; heavy injection backlogs it — poll
generously and treat "worker didn't finish in window" as SKIP, not failure.

## Recipe: validate a deploy path

- **One-click** (customer path): fresh QEMU VM on the staging host → `curl -sfL
  https://raw.githubusercontent.com/soctalk/soctalk/vX.Y.Z/install.sh | sudo -E
  bash -s -- --demo` with `SOCTALK_LLM_*`.
  The tag-pinned installer self-pins chart+images. **LLM env contract**:
  `SOCTALK_LLM_PROVIDER`, `SOCTALK_LLM_API_KEY`, and for
  `openai-compatible`/`self-hosted` **both** `SOCTALK_LLM_BASE_URL` and
  `SOCTALK_LLM_MODEL` (required — else defaults to `gpt-4o`, which a gateway
  404s). The tenant key propagates without a per-tenant key: the controller's
  `apply_secrets` falls back to `soctalk-system-llm-api-key` → `tenant-llm-key`.
  Default `--demo` runs on flannel with `preInstallCheck.enabled: false` →
  NetworkPolicy NOT enforced (PoC). Charts-only install needs an NP-CNI or the
  check disabled.
- **launchpad L2** (`soctalk-launchpad`): provisions MSSP + tenant VMs, joins a
  Tailscale overlay, runs `install.sh` over SSH. Pins the installer to a soctalk
  tag (`defaultInstallerURL`) — **bump in lockstep** on a new soctalk. Threads
  `SOCTALK_LLM_{PROVIDER,API_KEY,BASE_URL,MODEL}`. The tenant runs-worker reaches
  the MSSP over a self-signed cert and must honor `SOCTALK_API_VERIFY_SSL`
  (rendered from `soctalkSystem.verifySsl`) or run-claims fail
  `CERTIFICATE_VERIFY_FAILED`.
- **Playwright sweeps** run **outside** the target cluster. `domcontentloaded`
  (never `networkidle` — the app holds an open stream). CSRF over a port-forward
  needs `--host-resolver-rules=MAP <surface-hostname>:443 <listener-ip>:18443`
  (substitute the real surface hostname and the local port-forward listener
  address; the literal is a placeholder, not the string `host`). Assert no silent redirect
  / no page error / no 5xx / no non-allowlisted 403 / no error-boundary text, plus
  interactions — not just HTTP 200.
- **Pricing e2e**: `tests/e2e/pricing_enforcement.py` (env-contract). Real-run
  steps assert-if-completed / SKIP-if-slow.

## Access notes (resolve concrete targets from operator config)

- **Staging**: SSH to the staging host; `kubectl` against the staging apiserver
  (`--insecure-skip-tls-verify` for the self-signed control plane); Postgres via
  `kubectl -n soctalk-system exec soctalk-system-postgres-0 -- psql -U
  soctalk_admin -d soctalk`. Throwaway QEMU VMs live in a work dir on the staging
  host.
- **Demo**: SSH to the demo host as its admin user. Reach the MSSP UI via the
  MSSP-surface hostname (the customer-surface hostname auto-pins the demo tenant).
- **argon2 hashes contain `$`** — pipe password SQL via stdin, never inline in a
  double-quoted ssh command. Nested triple-SSH quoting mangles psql; write a
  script and scp it.

## Traps that waste time

- Overlay/MagicDNS resolvers negative-cache a new DNS name for tens of minutes
  (an ISP resolver in the mix); a new record looks dead locally while the
  authoritative nameserver already answers.
- `helm template`/`lint` passing is not proof; #142 defects rendered perfectly
  and were wrong at runtime. Verify against the real client stack semantics.
- `IntegrationConfig` is `table=True` SQLModel — pydantic validators don't run;
  validate at the API boundary.
- The provisioning controller reconciles tenant deployments — a manual
  `kubectl set env`/patch gets reverted; change config via the API (or chart +
  re-provision).
- macOS runners have no `timeout`; use the tool's own timeout / `gtimeout`.
  Redirect stdin from `/dev/null` for long background commands.

## Release & validation log (newest last — append every cut/gate)

| Version | Tag @ sha | What / why | Gated |
|---|---|---|---|
| 0.2.1 | `85921ec` | Original cut: #142 (22 Codex rounds) + uv.lock sync | staging by digest; sweeps; pricing e2e 12/12; real triage |
| 0.2.1 | `2e097ba` | + audience-wall (#143/#144), base-URL guard, demo MSSP-surface host | staging re-applied; demo redeployed; guards verified live |
| 0.2.1 | `90b576c` | + installer LLM model-knob (gateway → `gpt-4o` 404) | fresh QEMU one-click → real triage, no patching |
| 0.2.1 | `9a2a2dd` | + L2 runs-worker `SOCTALK_API_VERIFY_SSL` | launchpad L2 re-run → real triage on the tenant |
| 0.2.1 | `f29c5f8` | + installer alias-normalize + values-file skip (holistic review) | staging coherent by digest (reproducible build, no churn) |

## Known follow-ups

- **Separate the dev chart version from release versions** so a post-cut main
  push can't move the published `X.Y.Z` chart (touches the demo pipeline — not
  inside a release).
- **`helm push` semantics, stated precisely** (the workflow comments in
  `publish-images.yml`/`cut-k8s-release.yml` still call it "idempotent", which is
  true only for the identical-content case): re-pushing a chart version with the
  **same** rendered content is a no-op (same digest); re-pushing that version with
  **changed** content overwrites the tag to a new digest (GHCR allows it). That is
  the mutable-chart-tag reality rule 1 guards against. Not a pending code change —
  just don't read "idempotent" as "immutable".
