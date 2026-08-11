# SocTalk Install Guide

This guide walks an MSSP cluster admin through installing SocTalk and
onboarding the first end-customer.

## 1 Cluster prerequisites

Install these once per K3s cluster *before* `soctalk-system`:

SocTalk expects Kubernetes 1.30+ because the system chart installs a native
`ValidatingAdmissionPolicy` guard for tenant namespace operations.

### 1.1 K3s with Cilium

```bash
# K3s with flannel + kube-proxy disabled (Cilium will replace).
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC=" \
  --flannel-backend=none \
  --disable-network-policy \
  --disable-kube-proxy \
  --disable=traefik \
" sh -

# Install Cilium.
helm repo add cilium https://helm.cilium.io/
helm install cilium cilium/cilium --version 1.15.x \
  --namespace kube-system \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost=<node-ip> \
  --set k8sServicePort=6443 \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true

# Verify.
cilium status
```

### 1.2 cert-manager

```bash
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --version v1.14.x \
  --set installCRDs=true
```

Configure a `ClusterIssuer` appropriate for your environment (Let's Encrypt,
internal CA, or self-signed for dev). Example `letsencrypt-prod`:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: letsencrypt-prod }
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@your-mssp.example
    privateKeySecretRef: { name: letsencrypt-prod }
    solvers:
      - http01:
          ingress:
            class: traefik
```

### 1.3 Ingress controller

K3s does not ship Traefik with (we disabled it in §1.1). Install your
preferred ingress:

```bash
# Option A Traefik v3 (keep it if familiar)
helm repo add traefik https://traefik.github.io/charts
helm install traefik traefik/traefik -n ingress-system --create-namespace

# Option B: ingress-nginx
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx -n ingress-system --create-namespace
```

Label the ingress namespace for NetworkPolicy:

```bash
kubectl label namespace ingress-system managed-by=ingress
```

### 1.4 OIDC ingress (OAuth2-Proxy)

SocTalk does not implement login itself. Front it with OAuth2-Proxy (or
Keycloak / Dex) that terminates OIDC and forwards trusted identity headers.

```bash
helm repo add oauth2-proxy https://oauth2-proxy.github.io/manifests
helm install oauth2-proxy oauth2-proxy/oauth2-proxy -n ingress-system -f oauth2-proxy-values.yaml
```

Minimal `oauth2-proxy-values.yaml`:

```yaml
config:
  clientID: <your OIDC client ID>
  clientSecret: <your OIDC client secret>
  cookieSecret: <32-byte base64>
extraArgs:
  provider: oidc
  oidc-issuer-url: https://your-idp.example/
  upstream: static://202
  set-xauthrequest: true
  pass-authorization-header: true
  reverse-proxy: true
```

Configure your ingress to route `/oauth2/*` to OAuth2-Proxy and protect the
SocTalk UIs with an auth-snippet. Example for ingress-nginx:

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/auth-url: "https://$host/oauth2/auth"
    nginx.ingress.kubernetes.io/auth-signin: "https://$host/oauth2/start?rd=$escaped_request_uri"
    nginx.ingress.kubernetes.io/auth-response-headers: X-Auth-Request-User, X-Auth-Request-Email, X-Auth-Request-Groups
```

### 1.5 StorageClass

Any dynamic provisioner works. For K3s default: `local-path` is pre-installed.
For production: Longhorn, Rook/Ceph, or cloud-provider CSI. Ensure one is
marked `storageclass.kubernetes.io/is-default-class=true`.

## 2 Install SocTalk

### 2.1 Prepare values

Create `soctalk-system-values.yaml`:

```yaml
install:
  msspId: "<uuid>"         # generate: uuidgen | tr A-Z a-z
  msspName: "Your MSSP"
  installId: "<uuid>"
  installLabel: "pilot-prod"

image:
  registry: ghcr.io/soctalk
  tag: "0.2.1"

ingress:
  enabled: true
  className: nginx
  tls:
    issuerRef: letsencrypt-prod
    secretName: soctalk-tls
  hostnames:
    mssp: mssp.your-mssp.example
    customer: "*.customers.your-mssp.example"

oidc:
  trustedHeaderUser: X-Auth-Request-User
  trustedHeaderEmail: X-Auth-Request-Email
  trustedProxyCIDRs:
    - 10.42.0.0/16   # your pod CIDR / ingress CIDR

postgres:
  enabled: true
  storage: { size: 20Gi }
```

### 2.2 Install

```bash
helm install soctalk-system oci://ghcr.io/soctalk/charts/soctalk-system \
  --version 0.2.1 \
  --namespace soctalk-system --create-namespace \
  -f soctalk-system-values.yaml
```

The chart's pre-install Job verifies cluster prerequisites and fails fast if
any are missing.

### 2.3 Run migrations

```bash
# One-shot Alembic upgrade, run from inside the API pod or externally with
# admin credentials sourced from the generated Secret.
kubectl -n soctalk-system exec -it deploy/soctalk-system-api -- \
  alembic upgrade head
```

### 2.4 Seed the Organization row

```bash
kubectl -n soctalk-system exec -it deploy/soctalk-system-api -- \
  python -m soctalk.core.provisioning.bootstrap
```

### 2.5 Create an initial MSSP admin user

```bash
kubectl -n soctalk-system exec -it deploy/soctalk-system-api -- \
  soctalk-cli users create \
    --email admin@your-mssp.example \
    --role platform_admin
```

(The `soctalk-cli` entrypoint is planned; use the API until it lands.)

## 3 Onboard first customer

Log in at `https://mssp.your-mssp.example`. Navigate to **Customers → New
customer**. The wizard captures identity, LLM config, integration URLs, and
branding; provisioning runs asynchronously and the detail page streams
lifecycle events.

After the tenant reaches `active`:

1. Update the tenant's LLM API key via **Customer → Settings → LLM**.
2. Configure Wazuh agent ingress per
   [docs/multi-tenant/wazuh-ingress.md](../wazuh-ingress.md).
3. Share the customer UI URL and initial `customer_viewer` invite with the
   end-customer.

### Variant — provided-SIEM tenant

When the customer **already runs Wazuh** and SocTalk should analyze that
external SIEM rather than provision one in-cluster, choose the **`provided`**
profile on the **Profile** step. See
[provided-profile.md](../provided-profile.md) and
[wazuh-profiles.md](../wazuh-profiles.md) for the full contract.

Selecting `provided` reveals a conditional **External SIEM** wizard step (the
5th step: *Identity → Profile → External SIEM → Branding → Review*). It captures
the two credential pairs SocTalk needs to reach the external Wazuh — the
**indexer** (OpenSearch, `:9200`, used by the adapter for alert ingest) and the
manager **API** (`:55000`, used by the L1 chat resolver):

| Field | Meaning |
|---|---|
| `indexer_url` | External indexer base URL, e.g. `https://wazuh.customer.example:9200` |
| `indexer_username` / `indexer_password` | Indexer HTTP-Basic credentials |
| `api_url` | External Wazuh manager API base URL, e.g. `https://wazuh.customer.example:55000` |
| `api_username` / `api_password` | Manager API HTTP-Basic credentials |
| `api_token` *(optional)* | Pre-minted manager Bearer token; overrides username/password auth |
| `verify_ssl` | Uncheck for a self-signed external indexer/manager cert |

The wizard blocks submission until `indexer_url`, `indexer_username`,
`indexer_password`, `api_url`, `api_username`, and `api_password` are filled
(`api_token` is optional); the server independently rejects an incomplete
`provided` onboard with **HTTP 422**. On submit, the values persist onto the
tenant's `IntegrationConfig`, and provisioning writes them into
`Secret/tenant-external-siem-creds` in `tenant-<slug>` — **no** in-cluster
Wazuh/TheHive/Cortex and **no** agent ingress are deployed (skip step 2 above;
agents keep reporting to the customer's external manager).

Before the adapter can ingest, make sure both external hosts are reachable: the
tenant adapter's Cilium FQDN egress allow-list and the `soctalk-system`
control-plane egress to the manager are covered in
[provided-profile.md](../provided-profile.md) §5. Rotate the credentials later
from the tenant detail page's **External SIEM** panel (which calls
`PATCH /api/mssp/tenants/{id}/external-siem`).

## 4 Verify

```bash
# All soctalk-system pods Ready
kubectl -n soctalk-system get pods
# Tenant namespace exists and data plane is Ready
kubectl -n tenant-acme get pods
# No cross-tenant traffic (Hubble)
hubble observe --namespace tenant-acme --verdict DROPPED
```

## Next

- [Operator runbook](../runbook/README.md): common issues and fixes.
- [Upgrade guide](../upgrade/README.md): install-level + per-tenant upgrades.
- [wazuh-ingress Wazuh ingress](../wazuh-ingress.md): customer agent onboarding.
