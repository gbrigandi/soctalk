# SocTalk release workflow

How a SocTalk version is built, published, cut, and delivered. This is the
operational map behind the four GitHub Actions workflows in `.github/workflows/`
and the one-command installer.

> **This doc is a living record.** Keep it in sync as we release and validate:
> update the *Current release* line and the *Release & validation log* whenever a
> version is cut, re-cut, or gated on a box. The log is the running tab.

## Current release

- **`0.2.1` — git tag `v0.2.1` @ `f29c5f8`** (re-cut; see the log for why the tag
  moved). Contains: the full #142 pricing/engine work, the frontend
  audience-wall (#143/#144) + LLM base-URL-clear guard, the installer LLM
  model-knob + provider-alias normalization + values-file validation skip, the
  L2 runs-worker `SOCTALK_API_VERIFY_SSL` fix, the demo two-hostname topology,
  and `tests/e2e/pricing_enforcement.py`.
- **Gated on**: the NUC (all containers verified by digest), and validated to a
  real triage verdict on three deployment paths — one-click installer (fresh
  QEMU VM), launchpad L2 (MSSP + tenant across VMs), and the NUC gate. Demo
  tracks head via `deploy-demo`.
- **Open**: issue #145 (one-click flannel/no-NetworkPolicy PoC trade-off +
  charts-only NP-CNI requirement). A launchpad qemu-plugin false-cache-hit bug is
  known but not yet filed.

## The moving parts

| Artifact | Where it lives | Versioned how |
|---|---|---|
| Container images | `ghcr.io/soctalk/soctalk-{api,app-ui,orchestrator,adapter,linux-ep}` | `latest`, `<short-sha>`, and (on a cut) `X.Y.Z` |
| Helm charts (OCI) | `ghcr.io/soctalk/charts/{soctalk-system,soctalk-tenant,soctalk-cloud-agent,linux-ep}` | the `version:` in each `Chart.yaml` |
| Installer | `raw.githubusercontent.com/soctalk/soctalk/<ref>/install.sh` | pinned by the git ref in the URL (a tag = a pinned installer) |
| VM appliances | attached to the GitHub Release (`.ova`, `.qcow2.xz`, `.raw.xz`, `.vhd(x).xz`, `.vmdk.xz`) + `.deb`/`.rpm` | the release tag |

All GHCR packages are **public** (anonymous `helm pull` / image pull work); the
manual token+manifest REST calls are finicky but real OCI clients (helm,
containerd) resolve them fine.

## Two publish paths, deliberately separate

### 1. `publish-images.yml` — the moving dev line (every push to `main`)

- Trigger: `push` to `main`, with `paths-ignore` for `**.md`, `docs/**`,
  `tests/**`, `LICENSE`, `.gitignore` — a docs/test-only push does **not**
  republish.
- Publishes the **moving** `latest` + `<short-sha>` images **and the charts at
  their current `Chart.yaml` version**.
- Because all charts currently pin `version: 0.2.1`, every non-ignored main push
  **re-publishes the `0.2.1` chart** with new content. GHCR overwrites the tag
  (a chart tag is mutable). This is what auto-updates the demo via
  `deploy-demo.yml`.
- **Wart to know:** the dev chart version and the release version are the same
  string today. So pushing to `main` moves the published `0.2.1` chart away from
  whatever the cut produced. The git tag `vX.Y.Z` is the release of record; the
  published *chart* under that version can drift on a later main push. Images are
  safer — the version-tagged `X.Y.Z` images are only pushed by the cut
  (below), never by `publish-images`, so a version-tagged image is immutable
  between cuts.
- Consequence for verification: **a tag is not evidence — verify by digest.**
  With `pullPolicy: IfNotPresent` (chart default), a node keeps cached layers
  under an unchanged tag, so a deploy can "succeed" on stale bits.

### 2. `cut-k8s-release.yml` — a versioned release (manual, `workflow_dispatch` only)

Dispatch-only on purpose: creating the `vX.Y.Z` tag here must not re-trigger a
tag-driven build (no loop), and a release is a deliberate human action.

Inputs: `version` (no leading `v`, must match `charts/soctalk-system/Chart.yaml`)
and `create_release` (bool → the git tag + GitHub Release).

What it does, from the dispatched ref (`main`):
1. Re-builds the images from HEAD and tags them `X.Y.Z` + `latest` + `<short-sha>`.
2. `helm push`es the charts at their `Chart.yaml` version.
3. Creates the `vX.Y.Z` git tag and the GitHub Release (when `create_release`).
4. Fires the Packer VM-image build (`build-packer-images.yml`) via
   `workflow_dispatch`, async, so the k8s release + packages don't wait on it.

Permissions it needs: `contents: write` (tag + Release), `packages: write`
(GHCR), `actions: write` (dispatch the VM build).

> Verify the cut built HEAD, not the old tag. A cut dispatched against a stale
> ref rebuilds the wrong sha — that is how tag/artifact drift starts.

### 3. `build-packer-images.yml`

Builds the VM appliance images (OVA/qcow2/raw/vhd/vhdx/vmdk) and the `.deb`/`.rpm`,
attaches them to the release. Fired by the cut, runs async on its own runner.

### 4. `deploy-demo.yml` — the demo box

- Trigger: `workflow_run` after `publish-images` succeeds, or `workflow_dispatch`
  (with an optional `chart_version` input; defaults to `Chart.yaml`'s version).
- Over SSH to the demo host it runs a **full chart reconcile**:
  `helm upgrade --install soctalk-system oci://ghcr.io/soctalk/charts/soctalk-system
  --version <ver> -f deploy/demo-values.yaml --wait --atomic`. No local builds —
  pods pull from `ghcr.io/soctalk`.
- `deploy/demo-values.yaml` sets `image.pullPolicy: Always` because a moving tag
  is otherwise cached forever; a **forced rollout** step restarts the app
  deployments (not postgres) so the fresh tag is actually re-pulled.
- Then a smoke gate: sweep stale `ci-*` tenants → onboard smoke
  (`tests/e2e/smoke_onboard.py`, wizard → tenant ACTIVE → Wazuh live →
  decommission) → OpenAPI-client smoke. A red smoke fails the deploy.
- Demo tracks `latest` by default; to pin it to a release, set `image.tag` +
  the `tenantProvisioning.*ImageTag` fields to the version in the values.

## The one-command installer (`install.sh`)

The customer path and the appliance first-boot core (`infra/packer/scripts/firstboot.sh`
sources it). Pinned by fetching it from a tag:

```
curl -sfL https://raw.githubusercontent.com/soctalk/soctalk/vX.Y.Z/install.sh | sudo -E bash
```

- The tag-pinned installer self-pins the chart and images: `SOCTALK_CHART_VERSION`
  and `IMAGE_TAG` both default to the release version it shipped with.
- Installs k3s + Helm (if missing), then `helm install`s the published OCI
  `soctalk-system` chart. `--demo` = non-interactive, onboards a `demo` tenant.
- LLM env contract (unattended): `SOCTALK_LLM_PROVIDER`, `SOCTALK_LLM_API_KEY`,
  and — for `openai-compatible`/`self-hosted` — **both** `SOCTALK_LLM_BASE_URL`
  and `SOCTALK_LLM_MODEL` (required; without a model it would default to `gpt-4o`,
  which a gateway/self-hosted endpoint does not serve → every triage 404s).
- The tenant LLM key propagates without a per-tenant key: the provisioning
  controller's `apply_secrets` falls back to the install-wide
  `soctalk-system-llm-api-key` Secret and writes it into `Secret/tenant-llm-key`.
- Default `--demo` install runs on k3s **flannel** with `preInstallCheck.enabled:
  false`, so per-tenant NetworkPolicies are created but **not enforced** — a PoC
  trade-off. A raw charts-only install fails the pre-install check on default k3s
  ("requires an NP-enforcing CNI") unless you install Cilium/Calico or disable
  the check.

## Downstream: `soctalk-launchpad`

Provisions VMs across clouds/hypervisors, joins them to a Tailscale overlay, and
runs `install.sh` over SSH. It pins the installer to a specific soctalk tag
(`defaultInstallerURL = …/soctalk/vX.Y.Z/install.sh`) — **bump in lockstep** when
cutting a launchpad release for a newer soctalk. It threads
`SOCTALK_LLM_{PROVIDER,API_KEY,BASE_URL,MODEL}` through, so a gateway/self-hosted
run works with the installer's model requirement. Cross-cluster (L2) runs: the
tenant runs-worker reaches the MSSP API to claim runs and honors
`SOCTALK_API_VERIFY_SSL` (rendered from `soctalkSystem.verifySsl`) so a
self-signed L1 cert doesn't block claims.

## Recipe: cut a new release

1. Land everything on `main` (explicit paths; keep unrelated WIP out).
2. Ensure `charts/*/Chart.yaml` `version:` = the target `X.Y.Z`; sync `uv.lock`
   if the version bumped.
3. `publish-images` fires on the push and republishes the `X.Y.Z` chart —
   expected; the cut supersedes it.
4. If re-cutting an existing version: delete the tag + GitHub Release first
   (`gh release delete vX.Y.Z --yes; git push origin :refs/tags/vX.Y.Z; git tag -d vX.Y.Z`).
5. Dispatch `cut-k8s-release.yml --ref main -f version=X.Y.Z -f create_release=true`.
   Verify the run built **main's HEAD sha**.
6. Gate on the release box **by digest**, not by tag. On the NUC:
   `scripts/nuc-released-bits.sh X.Y.Z --apply` (system chart, `pullPolicy=Always`
   + forced rollout), then evict + restart tenant pods (they default
   `IfNotPresent`) and re-run until it exits 0.
7. A **test-/docs-only** follow-up push does not republish the chart
   (`paths-ignore`), so the cut stays intact; any other main push re-publishes
   the `X.Y.Z` chart at the new HEAD.

## Validation gates (what we run before trusting a cut)

Every cut is exercised, not assumed. The gates, in rough order:

- **Full pytest** (`uv run pytest`) — DB up on `localhost:5433`. A handful of
  RLS/response tests error without the test DB; those are environmental, proven
  by re-running at a clean HEAD in a throwaway worktree.
- **Digest gate on the NUC** — `scripts/nuc-released-bits.sh X.Y.Z` asserts every
  running container's imageID equals the registry-published digest; `--apply`
  reinstalls the system chart (`pullPolicy=Always` + forced rollout). Tenant
  pods default `IfNotPresent`, so evict the image (`k3s crictl rmi …`) + rollout
  restart, then re-run until it exits 0. **Reproducible builds**: an
  install.sh-/docs-only re-cut produces byte-identical image digests, so the NUC
  needs no re-apply after such a cut.
- **Frontend sweeps (Playwright, run OUTSIDE the box)** — MSSP + tenant route
  sweeps: not just HTTP 200 but no silent redirect, no page error, no 5xx, no
  non-allowlisted 403, no error-boundary text, plus interactions (open an
  investigation, Timeline→Replay, fleet replay, logout). Use `domcontentloaded`
  (`networkidle` never fires — the app holds an open stream). CSRF over a
  port-forward needs `--host-resolver-rules=MAP host:443 <ip>:18443`.
- **Pricing e2e** — `tests/e2e/pricing_enforcement.py` (env-contract, urllib):
  unpriced gate, served-engine invariant, override persistence + rate
  enforcement on a real run, accounting-off, consumption rollup, budget halt.
  Real-run steps assert when the worker finishes in-window and SKIP otherwise
  (single-replica worker throughput is not a pricing regression).
- **Real triage** — mint an adapter token inside the api pod
  (`mint_adapter_token`), `POST /api/internal/adapter/events` (schema v2, do NOT
  set `template_hash` — it's a memoization key that can skip the LLM), poll the
  investigation for a rendered verdict. Proves the LLM path end to end.
- **Deploy-path validation** — one-click installer in a fresh QEMU VM on the NUC;
  launchpad L2 (MSSP + tenant VMs on the tailnet); demo via `deploy-demo`. Each
  driven through to a real triage verdict.
- **Codex adversarial review** — every fix reviewed before commit; a fix loop
  runs until `VERDICT: DONE`. A holistic cross-fix pass at the end catches
  interaction bugs the per-fix reviews can't (e.g. installer alias vs chart
  schema, L2 TLS on the worker).

Boxes and access: NUC `ssh gbrigandi@100.102.223.8` (see the `nuc-*` memories /
`[[demo-box-topology]]`); demo `ssh root@demo.soctalk.ai`, two hostnames
(`demo.` = customer surface auto-pinned to the demo tenant; `mssp.` = the MSSP
surface). Throwaway QEMU VMs live under `~/oneclick-vm` / `~/lp-vms` on the NUC.

## Release & validation log

Running tab — newest last. Keep appending as we cut / gate.

| Version | Tag @ sha | What changed / why re-cut | Gated |
|---|---|---|---|
| 0.2.1 | `85921ec` | Original cut: #142 pricing/engine work (22 Codex rounds) + uv.lock sync | NUC by digest; MSSP/tenant sweeps; pricing e2e 12/12; real triage |
| 0.2.1 | `2e097ba` | + frontend audience-wall (#143/#144), base-URL-clear guard, demo `mssp.` hostname | NUC re-applied; demo redeployed; guards verified live |
| 0.2.1 | `90b576c` | + installer LLM model-knob (gateways got `gpt-4o` → 404) | fresh QEMU one-click → real triage, no patching |
| 0.2.1 | `9a2a2dd` | + L2 runs-worker honors `SOCTALK_API_VERIFY_SSL` (self-signed L1 blocked claims) | launchpad L2 re-run → real triage on the `acme` tenant |
| 0.2.1 | `f29c5f8` | + installer provider-alias normalization + values-file validation skip (holistic Codex review) | NUC coherent by digest (reproducible build, no churn) |

## Known follow-ups

- **Separate the dev chart version from release versions** so a post-cut main
  push can't move the published `X.Y.Z` chart (touches the demo pipeline — do it
  outside a release).
- The `publish-images` comment once claimed `helm push` is idempotent; it is not
  (GHCR overwrites). Corrected in the workflow; the mutable-tag reality stands.
- **File the launchpad qemu-plugin false-cache-hit bug** (reports a cache hit for
  a base image absent on disk → `qemu-img create` fails). Worked around by
  placing a valid image; not yet an issue.
