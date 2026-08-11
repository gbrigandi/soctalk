# SocTalk release workflow

How a SocTalk version is built, published, cut, and delivered. This is the
operational map behind the four GitHub Actions workflows in `.github/workflows/`
and the one-command installer.

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

## Known follow-ups

- **Separate the dev chart version from release versions** so a post-cut main
  push can't move the published `X.Y.Z` chart (touches the demo pipeline — do it
  outside a release).
- The `publish-images` comment once claimed `helm push` is idempotent; it is not
  (GHCR overwrites). Corrected in the workflow; the mutable-tag reality stands.
