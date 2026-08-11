# soctalk-system

**Status: V1 alpha.** Templates cover the MVP control plane. See `docs/multi-tenant/` for context.

## Purpose

Installs SocTalk itself: the MSSP-deployed control plane. One install per MSSP K3s cluster; serves all end-customers belonging to that MSSP.

Contains:

- SocTalk API (FastAPI)
- MSSP UI (SvelteKit)
- Customer UI (SvelteKit)
- Orchestrator (LangGraph + MCP subprocesses)
- Postgres (in-chart StatefulSet; externalizable)
- SocTalk controller ServiceAccount with cluster-scoped namespace verbs (for managing `tenant-*` namespaces)
- ValidatingAdmissionPolicy guard for SocTalk-managed tenant namespaces

Does **not** install:

- The per-customer SOC stacks: those come from `soctalk-tenant`, installed by SocTalk controller.
- CNI, cert-manager, ingress controller, StorageClass: cluster prerequisites installed separately.

## Cluster prerequisites

Must exist in the cluster **before** `helm install soctalk-system`:

1. **Kubernetes 1.30+** (K3s or equivalent) for the default `ValidatingAdmissionPolicy` guard.
2. **NetworkPolicy-enforcing CNI**: Cilium is the supported primary path (see `docs/multi-tenant/cni-networkpolicy.md`). Calico is a documented alternate.
3. **cert-manager** with a `ClusterIssuer` resolvable for TLS (Let's Encrypt / internal CA / self-signed for dev).
4. **Ingress controller**: Traefik (K3s default) or ingress-nginx.
5. **Dynamic StorageClass**: local-path, Longhorn, cloud-provider CSI, etc. PVCs will use default if `postgres.storage.storageClassName` is empty.

For local development, `scripts/dev-up.sh` at repo root brings up a `k3d` cluster with Cilium and cert-manager pre-installed.

## Install

```bash
helm install soctalk-system oci://ghcr.io/soctalk/charts/soctalk-system \
    --version 0.2.1 \
    --namespace soctalk-system --create-namespace \
    -f values.yaml
```

Schema-required values (`values.schema.json` rejects an install without these):

- `install.msspId` (UUID)
- `install.msspName` (string)
- `install.installId` (UUID)

Not schema-required, but you almost always want them — the chart defaults are
empty, so the UI is not reachable by hostname without them:

- `ingress.hostnames.mssp` (MSSP UI hostname)
- `ingress.hostnames.customer` (customer UI hostname, may be wildcard like `*.customers.example.com`)

### Secrets you must create first

`install.sh` creates these for you; a raw `helm install` does not. Create them
in the release namespace before installing, or the API will not start:

```bash
kubectl create namespace soctalk-system

# Bootstrap MSSP admin — keys MUST be `email` and `password`.
# Use --from-literal: the password is hashed EXACTLY as stored, and
# `--from-file` keeps the trailing newline that `echo pw > file` adds, which
# seeds an admin password nobody can type at the login form.
kubectl -n soctalk-system create secret generic soctalk-system-bootstrap-admin \
    --from-literal=email='admin@example.com' \
    --from-literal=password='<admin password>'

# LLM API key. Populate BOTH sub-keys (the chart selects by provider at
# runtime); point the same key file at both if you use one provider.
kubectl -n soctalk-system create secret generic soctalk-system-llm-api-key \
    --from-file=anthropic-api-key=/path/to/llm-key \
    --from-file=openai-api-key=/path/to/llm-key
```

Then reference them:

```bash
--set-string install.bootstrapAdmin.existingSecret=soctalk-system-bootstrap-admin \
--set-string llm.existingSecret=soctalk-system-llm-api-key
```

`install.msspId` and `install.installId` are UUIDs that identify this install.
Generate them once (`uuidgen`) and **keep them stable across upgrades** —
changing them re-identifies the install.

### LLM provider and model

Always set an explicit model. The chart default is `gpt-4o`; a non-OpenAI
gateway or self-hosted server does not serve it, so every triage fails with 404
while the install still reports healthy:

```bash
--set-string defaults.llm.provider=<provider> \
--set-string defaults.llm.model=<model that endpoint actually serves>
```

`defaults.llm.provider` accepts `openai-compatible`, `openai`, `anthropic`,
`azure`, `ollama`. (`self-hosted` is an `install.sh` alias only — with raw Helm
use `openai-compatible` for a self-hosted or gateway endpoint.)

For `openai-compatible` you must **also** set the endpoint, and the key must be
in the `openai-api-key` sub-key:

```bash
--set-string defaults.llm.baseUrl=<gateway URL>
```

### Installing on stock k3s

The pre-install hook checks for Cilium/Calico CRDs as a proxy for "an
NP-enforcing CNI". Stock k3s enforces standard NetworkPolicy through its
built-in controller but ships neither CRD, so the hook is a false negative
there — disable just the hook and the tenant NetworkPolicies still render and
are still enforced:

```bash
--set preInstallCheck.enabled=false
```

Those CRDs back only the optional FQDN-egress feature (`CiliumNetworkPolicy`),
which is capability-gated and no-ops without them. See
`docs/multi-tenant/cni-networkpolicy.md`.

k3s-specific values (these are environment choices, **not** chart
requirements — adjust for your cluster's ingress controller and storage):

```bash
--set-string ingress.className=traefik \
--set-string ingress.controllerNamespace=kube-system \
--set-string tenantProvisioning.persistentStorageClass=local-path
```

Image and tenant-chart versions already default to the chart's own version, so
you do not need to set `image.tag` or the `tenantProvisioning.*ImageTag` values
unless you are deliberately pinning something different.

#### Lab installs without TLS

To run without cert-manager over plain HTTP — **lab/PoC only, not a production
posture** — clear the issuer and allow non-secure cookies:

```bash
--set-string ingress.tls.issuerRef= \
--set auth.cookieSecure=false
```

Clearing `issuerRef` drops both the cert-manager annotation and the Ingress TLS
stanza. A production install keeps cert-manager (see Prerequisites), a real
`issuerRef`, and `auth.cookieSecure=true`.

**Consequence — the app's origin becomes `http://`.** With no TLS stanza the
chart renders `SOCTALK_PUBLIC_ORIGIN=http://<ingress.hostnames.mssp>`, and every
state-changing request (`POST`/`PUT`/`PATCH`/`DELETE`) carrying the session
cookie must present a matching `Origin`. So on a lab install you must reach the
UI/API over **http**, not https:

```bash
# correct on a no-TLS lab install
curl -b cookies -H "Host: soctalk.local" -H "Origin: http://soctalk.local" \
     -X POST http://<node>/api/mssp/tenants/onboard -d '{...}'
```

Using `https://` (even though the ingress still answers on 443 with Traefik's
default certificate) sends `Origin: https://soctalk.local`, which does not match
the rendered `http://…` origin, and the API rejects the request with
`403 {"detail":"CSRF validation failed"}` — while `GET`s and even login still
succeed, so it looks like a permissions bug rather than a scheme mismatch. If
you want https, install cert-manager and set a real `issuerRef` instead.

**Validation scope**: this recipe was validated end to end (real LLM triage
verdict) on stock k3s / Ubuntu 24.04 against `0.2.1`, over **plain HTTP with an
`anthropic` provider**. The production TLS path and the `openai-compatible`
provider path have not been validated charts-only and may need additional
settings.

## Upgrade

```bash
helm upgrade soctalk-system oci://ghcr.io/soctalk/charts/soctalk-system \
    --version 0.2.1 \
    --namespace soctalk-system \
    -f values.yaml
```

SocTalk's Alembic migrations run automatically on first API pod startup post-upgrade. Migrations are forward-only; rollback is via `helm rollback` plus Postgres restore if migrations introduced breaking data changes.

## Uninstall

```bash
helm uninstall soctalk-system --namespace soctalk-system
kubectl delete namespace soctalk-system
```

**Warning**: uninstalling destroys SocTalk's Postgres (including all tenant metadata). **Backup first.** V1 backup is manual (see `docs/multi-tenant/secret-placement.md` §6 and forthcoming install guide). `tenant-*` namespaces persist and must be cleaned separately via the SocTalk UI *before* uninstalling, or manually via `kubectl delete namespace tenant-*`.

## Files

```
charts/soctalk-system/
├── Chart.yaml
├── values.yaml
├── values.schema.json
├── README.md            (this file)
└── templates/
    └── .gitkeep
```
