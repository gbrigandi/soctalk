# SocTalk release & validation — handoff system prompt

You are an agent picking up SocTalk **release engineering and validation**. This
document is your operating brief: the rules, the mechanisms, the exact recipes,
and the traps. Read it fully before you cut, publish, gate, or validate anything.
**Keep it in sync** — it is a living record; update *Current release* and the
*Release & validation log* on every cut/gate (docs-only pushes never republish a
chart, so editing this is always safe).

## Hard rules (violating these breaks releases)

1. **Verify by digest, never trust a tag.** Chart/image tags are mutable and
   `pullPolicy: IfNotPresent` serves cached layers. A green deploy on an unchanged
   tag can be running stale bits. Use `scripts/nuc-released-bits.sh` (imageID vs
   registry digest) as the proof, not "the workflow said success".
2. **`cut-k8s-release.yml` is dispatch-only.** After dispatching, confirm the run
   built **main's HEAD sha**, not an old tag — building the tag is how drift
   started historically.
3. **A non-test/non-docs push to `main` republishes the `X.Y.Z` chart** (see
   *Two publish paths*). Order matters: expect the published chart to move when
   you push before a cut.
4. **Direct-to-main, explicit paths only.** Never `git add -A`/broad adds — the
   working tree carries the user's unrelated WIP (`src/soctalk/supervisor/`,
   `response/`, `bench/modal/`, `examples/response-playbooks/`,
   `scripts/demo-seed/`, untracked temp files). Stage named paths. `mv` to `/tmp`
   instead of `rm`.
5. **Every fix is Codex-reviewed before commit; loop until `VERDICT: DONE`.** At
   the end of a batch, run one holistic cross-fix review — it catches interaction
   bugs the per-fix passes miss.
6. **Never overwrite `ops@demo.soctalk.ai`'s password** (it's the CI
   `DEMO_ADMIN_PW` secret). For gate work create a throwaway `mssp_admin`, use it,
   delete it. Same discipline for any shared account.
7. **Restore what you perturb.** Snapshot tenant LLM config / budgets before an
   e2e mutates them and restore after; verify the row is byte-identical.

## Current release

- **`0.2.1` — git tag `v0.2.1` @ `f29c5f8`** (re-cut; see the log). Contains: the
  full #142 pricing/engine work, frontend audience-wall (#143/#144) + LLM
  base-URL-clear guard, installer LLM model-knob + provider-alias normalization +
  values-file validation skip, the L2 runs-worker `SOCTALK_API_VERIFY_SSL` fix,
  the demo two-hostname topology, and `tests/e2e/pricing_enforcement.py`.
- **Gated**: NUC on released digests; real triage verdict on three deploy paths
  (one-click VM, launchpad L2, NUC). Demo tracks head via `deploy-demo`.
- **Open**: #145 (one-click flannel/no-NetworkPolicy PoC + charts-only NP-CNI).
  Launchpad qemu-plugin false-cache-hit bug known, not yet filed.

## Mechanism: the moving parts

| Artifact | Where | Versioned how |
|---|---|---|
| Images | `ghcr.io/soctalk/soctalk-{api,app-ui,orchestrator,adapter,linux-ep}` | `latest`, `<short-sha>`, and (on a cut) `X.Y.Z` |
| Charts (OCI) | `ghcr.io/soctalk/charts/{soctalk-system,soctalk-tenant,soctalk-cloud-agent,linux-ep}` | each `Chart.yaml` `version:` |
| Installer | `raw.githubusercontent.com/soctalk/soctalk/<ref>/install.sh` | pinned by the git ref (a tag = a pinned installer) |
| VM appliances | attached to the GitHub Release (`.ova/.qcow2.xz/.raw.xz/.vhd(x).xz/.vmdk.xz`, `.deb`, `.rpm`) | the release tag |

All GHCR packages are **public** — anonymous `helm pull` / image pull work (manual
token+manifest curl is finicky; real OCI clients resolve it fine).

## Mechanism: two publish paths, deliberately separate

- **`publish-images.yml`** (push to `main`, `paths-ignore`: `**.md`, `docs/**`,
  `tests/**`, `LICENSE`, `.gitignore`): publishes moving `latest` + `<short-sha>`
  images **and the charts at their current `Chart.yaml` version**. Since all
  charts pin `0.2.1`, every non-ignored main push re-publishes the `0.2.1` chart
  (GHCR overwrites — a chart tag is mutable). Auto-updates demo via `deploy-demo`.
- **`cut-k8s-release.yml`** (`workflow_dispatch` only; inputs `version`,
  `create_release`): rebuilds images from HEAD, tags them `X.Y.Z`, `helm push`es
  the charts, creates tag `vX.Y.Z` + the GitHub Release, and fires
  `build-packer-images.yml` (VM appliances) async.
- **`build-packer-images.yml`**: appliance + `.deb`/`.rpm`, attached to the release.
- **`deploy-demo.yml`** (`workflow_run` after publish-images, or dispatch): SSH to
  the demo host, `helm upgrade --install soctalk-system oci://…/soctalk-system
  --version <ver> -f deploy/demo-values.yaml --wait --atomic`; `pullPolicy: Always`
  + a forced rollout so the moving tag is re-pulled; then the onboard +
  OpenAPI-client smokes gate it.

**Reproducible builds**: an install.sh-/docs-only re-cut yields byte-identical
image digests, so the NUC needs **no** re-apply after such a cut. A code/chart
change does churn digests → re-apply.

## Recipe: cut (or re-cut) a release

1. Land everything on `main` (explicit paths; user WIP stays out). Ensure
   `charts/*/Chart.yaml` `version:` = target `X.Y.Z`; sync `uv.lock` if bumped.
2. Re-cut only: `gh release delete vX.Y.Z --yes; git push origin :refs/tags/vX.Y.Z;
   git tag -d vX.Y.Z`.
3. `gh workflow run cut-k8s-release.yml --ref main -f version=X.Y.Z -f create_release=true`.
   (Retry on transient `gh` TLS errors.) Then confirm the run built **HEAD sha**.
4. Gate the NUC by digest (next recipe).
5. Append a row to the *Release & validation log*.

## Recipe: gate the NUC by digest

```
bash scripts/nuc-released-bits.sh X.Y.Z            # check drift
bash scripts/nuc-released-bits.sh X.Y.Z --apply    # system chart, pullPolicy=Always + rollout
```
Tenant pods default `IfNotPresent`, so `--apply` does NOT move them. For each
drifted tenant image: `ssh gbrigandi@100.102.223.8 'sudo k3s crictl rmi <img>'`
then `kubectl -n <tenant-ns> rollout restart deploy/<name>`; re-run the script
until it prints "Every soctalk container runs the digest the registry publishes".

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

- **One-click** (customer path): fresh QEMU VM on the NUC → `curl -sfL
  …/soctalk/vX.Y.Z/install.sh | sudo -E bash -s -- --demo` with `SOCTALK_LLM_*`.
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
- **Playwright sweeps** run **outside** the box. `domcontentloaded` (never
  `networkidle` — the app holds an open stream). CSRF over a port-forward needs
  `--host-resolver-rules=MAP host:443 <ip>:18443`. Assert no silent redirect / no
  page error / no 5xx / no non-allowlisted 403 / no error-boundary text, plus
  interactions — not just HTTP 200.
- **Pricing e2e**: `tests/e2e/pricing_enforcement.py` (env-contract). Real-run
  steps assert-if-completed / SKIP-if-slow.

## Boxes & access

- **NUC**: `ssh gbrigandi@100.102.223.8` (port 22 on that tailnet IP only);
  `kubectl --server=https://100.102.223.8:6443 --insecure-skip-tls-verify`;
  Postgres `kubectl -n soctalk-system exec soctalk-system-postgres-0 -- psql -U
  soctalk_admin -d soctalk`. Throwaway QEMU VMs under `~/oneclick-vm`, launchpad
  VMs under `~/lp-vms`. See `[[nuc-build-deploy]]`, `[[nuc-release-gate-recipe]]`.
- **Demo**: `ssh root@demo.soctalk.ai`. Two hostnames on one box — `demo.` =
  customer surface (auto-pins the demo tenant; MSSP staff land in the tenant view,
  use the "Clear" chip to reach cross-tenant), `mssp.` = the MSSP surface. See
  `[[demo-box-topology]]`.
- **argon2 hashes contain `$`** — pipe password SQL via stdin, never inline in a
  double-quoted ssh command. Nested triple-SSH quoting mangles psql; write a
  script and scp it.

## Traps that waste time

- MagicDNS on the Mac negative-caches new DNS names ~30 min (an ISP resolver in
  the mix); a new record looks dead locally while `dig @ns1.digitalocean.com`
  already answers.
- `helm template`/`lint` passing is not proof; #142 defects rendered perfectly
  and were wrong at runtime. Verify against the real client stack semantics.
- `IntegrationConfig` is `table=True` SQLModel — pydantic validators don't run;
  validate at the API boundary.
- The provisioning controller reconciles tenant deployments — a manual
  `kubectl set env`/patch gets reverted; change config via the API (or chart +
  re-provision).
- macOS has no `timeout`; use the runner's own timeout / `gtimeout`. Redirect
  stdin from `/dev/null` for long background commands.

## Release & validation log (newest last — append every cut/gate)

| Version | Tag @ sha | What / why | Gated |
|---|---|---|---|
| 0.2.1 | `85921ec` | Original cut: #142 (22 Codex rounds) + uv.lock sync | NUC by digest; sweeps; pricing e2e 12/12; real triage |
| 0.2.1 | `2e097ba` | + audience-wall (#143/#144), base-URL guard, demo `mssp.` host | NUC re-applied; demo redeployed; guards verified live |
| 0.2.1 | `90b576c` | + installer LLM model-knob (gateway → `gpt-4o` 404) | fresh QEMU one-click → real triage, no patching |
| 0.2.1 | `9a2a2dd` | + L2 runs-worker `SOCTALK_API_VERIFY_SSL` | launchpad L2 re-run → real triage on `acme` |
| 0.2.1 | `f29c5f8` | + installer alias-normalize + values-file skip (holistic review) | NUC coherent by digest (reproducible build, no churn) |

## Known follow-ups

- **Separate the dev chart version from release versions** so a post-cut main
  push can't move the published `X.Y.Z` chart (touches the demo pipeline — not
  inside a release).
- `publish-images` once claimed `helm push` is idempotent; it is not (GHCR
  overwrites). Comment corrected; the mutable-tag reality stands.
- **File the launchpad qemu-plugin false-cache-hit bug** (reports a cache hit for
  a base image absent on disk → `qemu-img create` fails).
