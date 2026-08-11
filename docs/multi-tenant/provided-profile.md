# provided-profile: the `provided` (bring-your-own Wazuh) tenant profile

The `provided` profile is the third onboarding shape, alongside `poc` and
`persistent` (see [wazuh-profiles.md](wazuh-profiles.md)). A `provided` tenant
brings their **own, externally-operated** Wazuh stack; SocTalk deploys **no**
in-cluster Wazuh / TheHive / Cortex for them. SocTalk's tenant namespace runs
only the alert **adapter** and the **runs-worker**, and the adapter is pointed
at the customer's external indexer.

This document is the operator contract for `provided`: when to pick it, what
lands in the cluster, the credential model, the credential lifecycle, the
network prerequisites, and the failure modes you will see.

## 1 When to choose `provided`

Choose `provided` when **the customer already runs Wazuh** (on-prem, in their
own cloud, or via a third party) and wants SocTalk to analyze the alerts that
SIEM already produces — not to stand up a second, SocTalk-managed Wazuh.

Pick `provided` when:

- The customer has an established Wazuh manager + indexer (OpenSearch) and a
  fleet of agents already enrolled against **their** manager.
- You must not duplicate ingestion, storage, or agent enrollment in the
  SocTalk cluster.
- You want the smallest per-tenant footprint (no Wazuh/TheHive/Cortex pods,
  no agent ingress, no per-tenant indexer PVCs).

Prefer `poc` or `persistent` instead when SocTalk should own the Wazuh stack
(demo tenants, or customers with no existing SIEM). The profile is fixed at
onboarding; switching profiles is a re-provision, not a toggle.

## 2 What SocTalk deploys (and what it does not)

For a `provided` tenant the tenant chart forces the in-cluster SOC components
**off** (`components.wazuh/thehive/cortex.enabled = false`) regardless of any
stale integration flag, and drops the Wazuh agent ingress. Only the adapter and
the runs-worker are deployed into `tenant-<slug>`.

| Resource in `tenant-<slug>` | `poc` / `persistent` | `provided` |
|---|---|---|
| Wazuh manager + indexer + dashboard | Deployed (in-cluster) | **Not deployed** — customer's external Wazuh |
| TheHive / Cortex | Deployed (when enabled) | **Not deployed** |
| Wazuh agent ingress (per-tenant hostname + TLS) | Deployed | **Not deployed** — agents report to the external manager |
| Wazuh bootstrap Secret (`tenant-bootstrap`) | Generated at provisioning | **Not generated** (no in-cluster Wazuh to seed) |
| Alert adapter (`soctalk-adapter`) | Deployed → in-cluster indexer Service | Deployed → **external** indexer URL |
| runs-worker | Deployed | Deployed |
| `Secret/tenant-external-siem-creds` | — | **Written by the controller** (both credential pairs) |
| Resource quota | poc 2/4 CPU · 4/8Gi · 20 pods · 4 PVC; persistent 2/5 CPU · 6/12Gi · 30 pods · 6 PVC | adapter+worker only: **1/2 CPU · 2/4Gi · 10 pods · 2 PVC** |

Indexer connection details for the adapter (`adapter.wazuhIndexer`) come from
`IntegrationConfig.wazuh_indexer_url` and the `tenant-external-siem-creds`
Secret; TLS verification flows from `IntegrationConfig.wazuh_verify_ssl` into the
adapter's `WAZUH_INDEXER_VERIFY_SSL` env var (allowing `verify=false` for a
self-signed external indexer).

## 3 Dual credential model

An external Wazuh exposes **two** independently-authenticated endpoints, so a
`provided` tenant carries **two HTTP-Basic credential pairs** plus one optional
token. All three live together in a single `Secret/tenant-external-siem-creds`
that mirrors the 4-key layout of the in-cluster `<release>-wazuh-creds` Secret,
so the adapter and chat resolver stay profile-agnostic.

| Endpoint | Default port | Used by | Auth | Secret keys |
|---|---|---|---|---|
| Indexer (OpenSearch) | `:9200` | Tenant **adapter** alert ingest | HTTP-Basic | `INDEXER_USERNAME` / `INDEXER_PASSWORD` |
| Manager API | `:55000` | L1 **chat resolver** | HTTP-Basic → mints a short-lived JWT, **or** a pre-minted Bearer token | `WAZUH_API_USERNAME` / `WAZUH_API_PASSWORD`, optional `WAZUH_API_TOKEN` |

- The **indexer** pair is HTTP-Basic against OpenSearch; the adapter uses it to
  pull alerts.
- The **API** pair is HTTP-Basic against the Wazuh manager. The chat resolver
  uses it to `POST /security/user/authenticate` and mint a short-lived JWT.
- `WAZUH_API_TOKEN` is **optional**: when present it is used directly as the
  Bearer token (token precedence), skipping the username/password mint. Its
  absence is always valid — the key is omitted from the Secret entirely when the
  column is null, so no empty env var materializes.

The two pairs are deliberately distinct: rotating indexer credentials need not
touch manager-API credentials and vice-versa.

## 4 Credential lifecycle

Credentials enter and move through the system in one direction:
onboard → `tenant-external-siem-creds` Secret materialization → PATCH endpoint →
adapter restart.

1. **Onboard.** `POST /api/mssp/tenants/onboard` with `profile: "provided"` and
   a nested `external_siem` block (`indexer_url`, `indexer_username`,
   `indexer_password`, `api_url`, `api_username`, `api_password`, optional
   `api_token`, `verify_ssl`). Missing required fields are rejected **422**
   server-side with field-level errors and **no** Tenant row is created
   (`api_token` / `verify_ssl` never trigger 422). The same 422 also covers a
   missing/blank `llm_api_key` (see the LLM key subsection below) — errors for
   both blocks are combined into one response so the wizard surfaces every
   missing field in a single round-trip. The values persist onto the
   tenant's `IntegrationConfig` in the same transaction as the Tenant row:
   `indexer_*` → `wazuh_indexer_*`, `api_username/api_password/api_url` →
   `wazuh_username` / `wazuh_password_plain` / `wazuh_api_url`, `api_token` →
   `wazuh_api_token_plain`, `verify_ssl` → `wazuh_verify_ssl`.
2. **Secret materialization.** During provisioning the controller's
   `_step_write_external_siem_secret` writes `Secret/tenant-external-siem-creds`
   in `tenant-<slug>` from those columns, keyed `INDEXER_USERNAME`,
   `INDEXER_PASSWORD`, `WAZUH_API_USERNAME`, `WAZUH_API_PASSWORD`, and
   `WAZUH_API_TOKEN` (token key only when set). The step is idempotent
   (create-or-patch) and runs **before** the tenant chart's adapter pod starts.
   No `tenant-bootstrap` Secret is minted, and `_step_write_integration_config`
   is a no-op so the external `wazuh_url` / `wazuh_indexer_url` are never
   clobbered with in-cluster Service URLs.
3. **Rotation via the PATCH endpoint.** `PATCH /api/mssp/tenants/{id}/external-siem`
   (profile-agnostic, all-optional fields; only non-null fields are written)
   updates the `IntegrationConfig` SIEM columns. **Postgres is committed first**
   (the dual-write mirrors `llm_config.update_tenant_llm`), then the endpoint
   rewrites `tenant-external-siem-creds` from the re-read row. A matching
   `GET /api/mssp/tenants/{id}/external-siem` returns a masked shape
   (`has_indexer_password`, `has_api_password`, `has_api_token` booleans);
   plaintext passwords/token are never returned.
4. **Adapter restart.** `secretKeyRef` env vars do **not** refresh on a Secret
   update, so after rewriting the Secret the endpoint patches the
   `soctalk-adapter` Deployment's pod-template annotation
   `soctalk.io/restartedAt`, which rolls the adapter pod onto the new indexer
   credentials. The chat resolver reads the API credentials **live per request**
   and needs no restart. The K8s side effects (Secret write + adapter restart)
   are best-effort: failures are logged via structlog and do **not** roll back
   the committed Postgres update.

### Per-tenant LLM key lifecycle

A `provided` tenant also brings its **own LLM credential** — the profile has
no install-shared fallback contract, so the onboard payload's `llm_api_key`
is **REQUIRED** alongside `external_siem`. A missing/blank key is rejected
with the same field-level **422** shape as the SIEM fields, before any DB
read/write, so a rejected onboard creates **no** Tenant row. `llm_provider`
is optional (`openai` | `anthropic` | `openai-compatible`; `openai` is
canonicalized to `openai-compatible` for storage); when omitted alongside a
key it is inferred from the key's vendor prefix (`sk-ant-…` → `anthropic`,
anything else keeps the `openai-compatible` default). For `poc` /
`persistent` both fields are optional in the onboard payload and the
install-shared key fallback below still applies. The key moves through the
system in the same one-way direction as the SIEM credentials:

1. **Onboard.** `llm_api_key` persists onto
   `IntegrationConfig.llm_api_key_plain` in the same transaction as the
   Tenant row. The raw key is never logged or echoed in any response — reads
   return only a `has_api_key` boolean and a masked preview.
2. **Secret materialization.** During the provisioning `apply_secrets` step —
   **before** `helm_apply_tenant`, so the runs-worker Deployment is never
   created without the Secret it mounts — the controller writes
   `Secret/tenant-llm-key` in `tenant-<slug>`. The per-tenant key takes
   precedence: when `IntegrationConfig.llm_api_key_plain` is set it is used
   verbatim and the install-shared Secret
   (`soctalk-system/soctalk-system-llm-api-key`) is **not** read; only
   key-less tenants get a mirror of the install-shared key.
   When neither source yields a key, provisioning fails fast with a
   `llm_key_missing` lifecycle event rather than stranding the runs-worker.
3. **runs-worker mount.** The tenant chart's `llm.apiKeyRef` points the
   runs-worker at `tenant-llm-key` (same-namespace `secretKeyRef`).
4. **Rotation via the PATCH endpoint.** `PATCH /api/mssp/tenants/{id}/llm`
   (the detail page's **LLM** panel drives the same endpoint). **Postgres is
   committed first**, then the endpoint rewrites the Secret in **both**
   namespaces — the mounted `tenant-<slug>/tenant-llm-key` and the
   legacy/audit copy `soctalk-system/tenant-<id>-llm` — and rolling-restarts
   the runs-worker (`secretKeyRef` env vars do **not** refresh on a Secret
   update). The K8s side effects are best-effort and never roll back the
   committed Postgres update. Chart-affecting edits in the same PATCH
   (provider / base_url / model / fast_model / reasoning_model) additionally
   enqueue a `tenant.reconcile` job for an active tenant so the new values
   reach the rendered release — see the [runbook](runbook/README.md).

### Per-tenant fast / thinking model overrides

Beyond the single primary `llm_model`, a tenant may pin **per-role** models
for the runs-worker's two LLM tiers. Terminology mapping, stated once and
used consistently across the stack: UI **"Thinking model"** == API
`reasoning_model` == column `llm_reasoning_model` == env
`SOCTALK_REASONING_MODEL` (and likewise UI "Fast model" == API `fast_model`
== column `llm_fast_model` == env `SOCTALK_FAST_MODEL`). The two roles at
runtime:

- **Fast model** (`SOCTALK_FAST_MODEL`) drives routing and worker-step
  calls — the supervisor's routing decision and HIL inquiry generation.
- **Thinking model** (`SOCTALK_REASONING_MODEL`) drives **verdict
  synthesis** — the supervisor's final verdict call.

**Onboard fields.** `POST /api/mssp/tenants/onboard` accepts optional
`llm_fast_model` / `llm_reasoning_model` for **every profile** (`poc` /
`persistent` / `provided` alike — unlike `external_siem` they are not
profile-gated). Blank/whitespace-only values normalize to "no override" so
the `IntegrationConfig` columns stay NULL; non-blank values persist in the
**same transaction** as the Tenant row. The server never defaults an
omitted override to a concrete model. When the onboard also resolves a
provider (explicit `llm_provider`, or inferred from the key's `sk-ant-`
prefix), a **clearly-mismatched** override is flipped to that provider's
default exactly like `llm_model` — a `gpt-*`/`o1*`/`o3*` override under
`anthropic`, or a `claude*` override under `openai-compatible` — while a
matching custom override is preserved verbatim.

**Fallback chain (render → chart → runtime).** Each model the runs-worker
uses resolves through three layers:

1. **Override column** — `render.py` sets `runsWorker.fastModel` /
   `runsWorker.reasoningModel` to the override **or** `llm_model`. NULL
   *and* empty string both fall back (a cleared override may be stored
   either way), so every pre-override tenant row renders exactly as before.
2. **Primary `llm_model`** — the value just resolved lands in the rendered
   values; the tenant chart additionally guards with
   `| default .Values.llm.model` when emitting the env vars.
3. **runs-worker env** — the chart's runs-worker template emits
   `SOCTALK_FAST_MODEL` (routing/workers) and `SOCTALK_REASONING_MODEL`
   (verdict synthesis); the worker's `load_config()` reads them at startup.

**PATCH tri-state contract.** `GET /api/mssp/tenants/{id}/llm` returns
nullable `fast_model` / `reasoning_model` (`null` = no override — falls
back to `model`). `PATCH /api/mssp/tenants/{id}/llm` treats both fields as
tri-state:

| Payload field value | Effect |
|---|---|
| omitted / `null` | stored override **unchanged** |
| `""` (or whitespace-only) | **clear** the override to NULL — revert to the primary `model` fallback |
| any other string | **set** verbatim |

The empty-string-clears convention exists because a changed-fields-only
PATCH cannot express "revert to the primary model" with `null`.

**Override changes are chart-affecting.** The resolved models are baked
into the rendered release's env values at helm-render time, so the
Secret-rewrite + pod-restart fast path that handles `api_key` rotation
**cannot** propagate them. Any change to either override — including a
clear — counts as chart-affecting exactly like a provider / base-URL /
model edit and enqueues a provisioning job: `tenant.reconcile` for an
**active** tenant, `tenant.provision` for every other state (e.g. the
degraded → provisioning retry route). If a pending/in-flight job of the
same kind already exists, no duplicate is enqueued — the existing job reads
the latest `IntegrationConfig` row when it runs. See the
[runbook](runbook/README.md) for the `tenant.reconcile` failure path.

**Provider-flip interaction.** Wherever the system itself flips a tenant's
provider — onboard key-prefix inference (`sk-ant-…` → `anthropic`), an
explicit onboard `llm_provider`, or the provisioning controller's auto-flip
when the install-shared key mirror picks the other vendor's key — the same
`reconcile_provider_model` helper is applied to non-NULL overrides exactly
as it is to `llm_model`: a clearly-mismatched override flips to the new
provider's default, matching custom models are preserved, and NULL
overrides **stay NULL** (the flip never materializes a concrete model into
an unset override, so the render-time `llm_model` fallback keeps working).
A direct `PATCH /llm` provider change is taken verbatim — the operator is
explicit there and is expected to adjust the models in the same PATCH.

## 5 Connectivity prerequisites

Because the SIEM is outside the cluster, **two** egress paths must be open:

1. **Tenant adapter → external indexer (FQDN egress).** The tenant adapter pod
   must reach the external indexer (`:9200`) and is constrained by a Cilium
   `CiliumNetworkPolicy` (`adapter-fqdn-egress`) FQDN allow-list. `render.py`
   populates `networkPolicies.externalSiemHosts` with the deduped hostnames
   parsed from `wazuh_indexer_url` **and** `wazuh_api_url`, and forces
   `networkPolicies.fqdnEgress.enabled = true`, so the rendered policy lists both
   the L1 control-plane host and every external SIEM host under `toFQDNs`. If a
   host is missing from the allow-list the adapter's egress is dropped.
2. **Control-plane egress → external manager (for chat).** The L1 chat resolver
   runs in `soctalk-system` and connects to the external manager API (`wazuh_api_url`,
   `:55000`). `charts/soctalk-system` must permit the API/controller pod to reach
   that manager host (a `CiliumNetworkPolicy` or equivalent). When the
   `soctalk-system` egress is unrestricted (e.g. a local dev profile with Cilium
   disabled) this is a documented no-op.

DNS for both external hostnames must resolve from inside the cluster, and the
external Wazuh's firewall must admit the cluster's egress IPs.

### 5.1 On a non-Cilium cluster (stock k3s), and the port constraint

The FQDN policy above is Cilium-only and is capability-gated, so on stock k3s it
renders as a no-op. What actually applies there is the **standard**
`NetworkPolicy/adapter-egress`, which `render.py` populates with an `ipBlock`
for the external SIEM host. That part works — but the policy's **ports are
hardcoded to `9200` and `55000`**; only the *host* is derived from your URLs.

**An external Wazuh reachable on any other port is dropped at L3/L4**, even
though the tenant reaches `active`, the credentials Secret is created, and the
adapter heartbeats normally. Verified side by side on one cluster, changing only
the ports:

| external URLs | adapter log |
|---|---|
| `:31437` / `:30442` (NodePort) | `ingest_failed: All connection attempts failed` |
| `:9200` / `:55000` | `ingest_failed: Client error '401 Unauthorized' …/_search` (i.e. it connected) |

So a BYO Wazuh behind a NodePort, load balancer, or reverse proxy on
non-standard ports will not ingest until the policy learns the port. Tracked in
issue #147. Until then, expose the external indexer/manager on `:9200`/`:55000`
to the cluster, or relax the tenant egress policy for that host.

### 5.2 What needs no manual step in 0.2.1

The controller creates `Secret/tenant-external-siem-creds` (all four keys) as
part of provisioning a `provided` tenant. **Do not hand-create it** — onboarding
with `external_siem` is sufficient, and a `PATCH .../external-siem` rewrites it
and rolls the adapter. (A stale comment in `render.py` describes this Secret as
"created by a later feature"; that feature has shipped.)

## 6 Failure modes

Two failure classes dominate `provided`'s external SIEM path, both surfaced
to the operator through the tenant detail page's **External SIEM** panel
(which polls `GET /api/mssp/tenants/{id}/adapter-status`) and the tenant
lifecycle events. The per-tenant LLM credential adds two more, surfaced
through failed runs and the detail page's **LLM** panel.

| Symptom | Likely cause | How the operator surfaces / fixes it |
|---|---|---|
| **Authentication failure** | Wrong/expired indexer or API credentials; rotated on the customer side; `WAZUH_API_TOKEN` expired | Adapter ingest returns 401/403 → `adapter-status.last_ingest_error` shows the auth error; the chat resolver surfaces `external Wazuh API not configured` (a typed `ExternalSiemNotConfigured`, never an unhandled 500). Fix by `PATCH /api/mssp/tenants/{id}/external-siem` with fresh creds (which rolls the adapter). |
| **Network unreachable** | On a non-Cilium cluster: the external SIEM is **not on `:9200`/`:55000`** — the standard egress policy hardcodes those ports (§5.1, issue #147). Otherwise: FQDN allow-list missing the host; external Wazuh down; DNS or firewall blocking; wrong URL | Adapter / resolver connection times out → `adapter-status` returns `{"reachable": false, "error": "<msg>"}`. Fix by confirming `externalSiemHosts` covers both URLs, the `soctalk-system` egress reaches `:55000`, DNS resolves, and the external Wazuh is up. |
| **401 from the indexer with credentials you believe are correct** | If your external Wazuh was deployed from this repo's `charts/wazuh`: the chart mints an `INDEXER_PASSWORD` into `*-wazuh-creds` but **never applies it to the indexer** — the indexer still only accepts the Wazuh image's built-in default, so the minted password is rejected (issue #147). Verify with `curl -sk -u <user>:<pw> https://<indexer>:9200/` before assuming SocTalk is at fault, and onboard with credentials the indexer actually accepts. | Adapter logs `ingest_failed: Client error '401 Unauthorized' for url '…/wazuh-alerts-*/_search'` while heartbeat stays `ok`. |
| **TLS verification failure** | Self-signed external indexer/manager cert with verification on | Connection fails on cert validation. Set `verify_ssl = false` (→ `WAZUH_INDEXER_VERIFY_SSL=false` and resolver `verify=false`) via onboard or the PATCH endpoint when the external cert is self-signed. |
| **Invalid LLM key** | Wrong/revoked `llm_api_key` supplied at onboard; rotated on the provider side | Provisioning still **succeeds** — the key is not validated at provision time. Runs fail at runtime with provider 401s from the runs-worker's LLM calls. Fix via the detail page's **LLM** panel or `PATCH /api/mssp/tenants/{id}/llm` (which rewrites the Secret and rolls the runs-worker). |
| **LLM endpoint unreachable** | Custom `llm_base_url` host missing from the rendered egress allow-list | `render.py` derives `networkPolicies.allowedLlmHosts` from the host portion of `llm_base_url`; a policy rendered before a base-URL change drops the runs-worker's LLM egress. A `tenant.reconcile` re-render updates the allow-list (enqueued automatically when a chart-affecting `PATCH /llm` lands on an active tenant). |

The credentials themselves never appear in logs or API responses; only presence
booleans and masked URLs/usernames are returned. See
[secret-placement.md](secret-placement.md) §2 for the Secret inventory row.
