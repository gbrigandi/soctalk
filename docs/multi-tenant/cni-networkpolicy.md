# cni-networkpolicy: CNI + NetworkPolicy Design

Gate artifact: Chooses the Container Network Interface for this release, defines the NetworkPolicy matrix between `soctalk-system` and `tenant-*` namespaces, and specifies FQDN egress rules for BYO LLM endpoints.

## 1 Decision: Cilium as primary CNI

Cilium is the supported CNI for SocTalk. Rationale:

1. **FQDN egress, not baseline enforcement**. A common misconception is that stock k3s cannot enforce `NetworkPolicy`. It can: flannel-the-CNI does not implement `NetworkPolicy`, but k3s ships a *separate* kube-router-based NetworkPolicy controller that enforces standard `NetworkPolicy` out of the box (unless started with `--disable-network-policy`). This is verified empirically — on a stock k3s node with no Cilium/Calico, a default-deny ingress policy takes a pod from reachable (HTTP 200) to blocked. So **baseline tenant isolation via standard `NetworkPolicy` holds on stock k3s**; Cilium is not required for it. Cilium is preferred for the reason below, not because k3s leaves tenants unisolated.
2. **FQDN egress policies**: standard `NetworkPolicy` permits only IP/CIDR-based egress. BYO LLM endpoints are hostnames (`api.openai.com`, customer-self-hosted endpoints behind CDNs with dynamic IPs). Cilium's `CiliumNetworkPolicy` with `toFQDNs` matches hostnames. This is the only way to enforce per-tenant LLM egress at the network layer without introducing a forward proxy.
3. **eBPF-based enforcement**: higher performance, lower latency, no iptables bloat.
4. **Observability (Hubble)**: flow-level visibility; operationally useful for debugging tenant isolation.
5. **Maturity**. CNCF Graduated, widely deployed in production.

### Alternate install mode: Calico + egress proxy

MSSPs with an operational mandate to run Calico can use with the following adjustment:
- Standard K8s `NetworkPolicy` (Calico-enforced) for all east-west and coarse egress.
- An **egress proxy** (Envoy, HAProxy, or Squid) in `soctalk-system` namespace that does FQDN-based allowlisting.
- `NetworkPolicy` restricts tenant pods and SocTalk orchestrator to egress **only through the proxy** for external (non-cluster) destinations.

This alternate is documented but is not the recommended path. It adds one component, one failure point, and inter-tenant shared resource (the proxy). If an MSSP selects it, SocTalk's design spike validates it end-to-end on their cluster before onboarding.

## 2 Install requirements

Cilium is a **cluster prerequisite** (see `chart-audit.md` §4). The `soctalk-system` chart does not install Cilium. The install guide's prerequisite section specifies:

```bash
# K3s without flannel and without default NP:
curl -sfL https://get.k3s.io | sh -s - server \
    --flannel-backend=none \
    --disable-network-policy \
    --disable=traefik  # if using a different ingress controller

# Install Cilium:
helm repo add cilium https://helm.cilium.io/
helm install cilium cilium/cilium --version 1.15.x \
    --namespace kube-system \
    --set operator.replicas=1 \
    --set ipam.mode=kubernetes \
    --set kubeProxyReplacement=strict \
    --set k8sServiceHost=<node-ip> \
    --set k8sServicePort=6443 \
    --set hubble.relay.enabled=true \
    --set hubble.ui.enabled=true
```

The `soctalk-system` chart's pre-install hook verifies an NP-enforcing CNI is present (it looks for Cilium or Calico CRDs) and fails fast if not.

### Charts-only install on stock k3s

The pre-install hook's CRD check is a proxy for "an NP-enforcing CNI is present", and it is **stricter than baseline isolation requires**: it looks specifically for Cilium/Calico CRDs, which back the *optional* FQDN-egress feature (§1, reason 2), not the standard tenant `NetworkPolicy` isolation. Stock k3s enforces the standard policies (verified above) but has neither CRD, so a raw `helm install` fails the hook:

```
soctalk-system-preinstall-check: ERROR: neither Cilium nor Calico CRDs found.
       SocTalk requires an NP-enforcing CNI.
```

On stock k3s this is a false negative. To proceed, disable just the pre-install hook — the standard tenant NetworkPolicies still render and are still enforced by k3s:

```bash
helm install soctalk-system oci://ghcr.io/soctalk/charts/soctalk-system \
    --version <ver> --set preInstallCheck.enabled=false   # + your other values
```

The one-click `install.sh` already renders `preInstallCheck.enabled: false` for exactly this reason. The trade-off of running without Cilium: no FQDN-based egress control (§1, reason 2) — per-tenant LLM egress is allowed by IP/CIDR, not by hostname. If you need hostname-level egress enforcement, install Cilium first (§2 recipe) and leave the hook enabled. A future chart release will broaden the hook to accept any proven NP enforcer (including stock k3s) rather than gating on a specific CNI's CRDs.

## 3 NetworkPolicy architecture

Default-deny baseline on every namespace SocTalk manages. Allow rules added explicitly for each legitimate flow.

### 3.1 Flows that must work

| Source | Destination | Why |
|---|---|---|
| `soctalk-system` → `tenant-<slug>` (e.g., Wazuh :55000, TheHive :9000, Cortex :9001) | East-west | SocTalk orchestrator's MCP subprocesses call tenant data plane APIs |
| `tenant-<slug>` (adapter) → `soctalk-system` (SocTalk API :8000) | East-west | Adapter reports health and pulls config |
| `soctalk-system` → external per-tenant LLM FQDN | Egress | LLM calls during triage (using tenant's LLM key under worker context) |
| External Wazuh agents → `tenant-<slug>` Wazuh manager (:1514, :1515) | Ingress | Customer endpoint telemetry |
| MSSP users → `soctalk-system` (via Ingress :443) | Ingress | MSSP UI + Customer UI access |
| `soctalk-system` Postgres ↔ `soctalk-system` (itself) | Intra-ns | SocTalk components talking to DB |
| `soctalk-system` → external OIDC provider | Egress | Ingress-level OIDC; flows via ingress-system ns |
| Tenant pods intra-namespace (manager↔indexer, TheHive↔Cassandra, etc.) | Intra-ns | Normal stack operation |

### 3.2 Flows that must be blocked (default-deny catches these)

- `tenant-acme` → `tenant-beta` (any port, any protocol)
- `tenant-<slug>` → internet (other than its configured LLM FQDN)
- `tenant-<slug>` → `soctalk-system` Postgres directly (adapter uses SocTalk API, not DB)
- Any namespace → `kube-system` beyond standard resolver queries
- Cross-cluster lateral movement from any compromised pod

## 4 NetworkPolicy templates

### 4.1 `soctalk-system` namespace policies

Managed by `soctalk-system` chart. Four policies:

**4.1.1 Default-deny all ingress/egress**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: default-deny, namespace: soctalk-system }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
```

**4.1.2 Allow SocTalk API to receive from Ingress controller + adapters**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: api-ingress-allow, namespace: soctalk-system }
spec:
  podSelector: { matchLabels: { app.kubernetes.io/name: soctalk-api } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: ingress-system }
      ports: [{ port: 8000, protocol: TCP }]
    - from:
        - namespaceSelector:
            matchLabels: { managed-by: soctalk, tenant: "true" }
      ports: [{ port: 8000, protocol: TCP }]
```

**4.1.3 Allow orchestrator to reach tenant namespaces + DNS + LLM FQDNs**

This is a `CiliumNetworkPolicy` because vanilla NP can't express FQDN egress:

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata: { name: orchestrator-egress, namespace: soctalk-system }
spec:
  endpointSelector:
    matchLabels: { app.kubernetes.io/name: soctalk-orchestrator }
  egress:
    # DNS
    - toEndpoints:
        - matchLabels:
            "k8s:io.kubernetes.pod.namespace": kube-system
            "k8s:k8s-app": kube-dns
      toPorts:
        - ports: [{ port: "53", protocol: UDP }]
          rules:
            dns:
              - matchPattern: "*"
    # Tenant data plane APIs (any tenant-* namespace, specific ports)
    - toEndpoints:
        - matchLabels:
            "k8s:io.kubernetes.pod.namespace-label:managed-by": soctalk
            "k8s:io.kubernetes.pod.namespace-label:tenant": "true"
      toPorts:
        - ports:
            - { port: "55000", protocol: TCP }  # Wazuh manager API
            - { port: "9200",  protocol: TCP }  # Wazuh indexer
            - { port: "9000",  protocol: TCP }  # TheHive
            - { port: "9001",  protocol: TCP }  # Cortex
    # Postgres (intra-ns)
    - toEndpoints:
        - matchLabels: { app.kubernetes.io/name: soctalk-postgres }
      toPorts: [{ ports: [{ port: "5432", protocol: TCP }] }]
    # LLM endpoints. FQDN allow-list is composed dynamically
    # (see §4.2: one CiliumNetworkPolicy per tenant maintained by SocTalk controller)
```

**4.1.4 Allow Postgres intra-ns only**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: postgres-ingress, namespace: soctalk-system }
spec:
  podSelector: { matchLabels: { app.kubernetes.io/name: soctalk-postgres } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector: {}  # any pod in soctalk-system
      ports: [{ port: 5432, protocol: TCP }]
```

### 4.2 Per-tenant LLM FQDN egress (dynamic)

SocTalk controller renders a `CiliumNetworkPolicy` per tenant that allows orchestrator → that tenant's LLM FQDN. When a tenant's LLM config changes, the policy is updated; when a tenant is decommissioned, the policy is deleted.

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: orchestrator-llm-egress-tenant-acme
  namespace: soctalk-system
  labels:
    managed-by: soctalk
    tenant-id: "<acme-uuid>"
spec:
  endpointSelector:
    matchLabels: { app.kubernetes.io/name: soctalk-orchestrator }
  egress:
    - toFQDNs:
        - matchName: "api.openai.com"  # or tenant's configured endpoint
      toPorts: [{ ports: [{ port: "443", protocol: TCP }] }]
```

Multiple per-tenant policies are additive. Each allows only that tenant's FQDN. When orchestrator is processing tenant `acme`'s triage, only `acme`'s FQDN is reachable from Cilium's perspective because Cilium allows any FQDN listed in any matching policy. **This does not enforce per-tenant FQDN isolation at the request level**: that's the application's responsibility (per-tenant LLM config, tenant-scoped cache keys). Network layer reduces blast radius if application logic has a bug.

### 4.3 Tenant namespace policies

Rendered by `soctalk-tenant` chart per tenant. Four policies per namespace:

**4.3.1 Default-deny**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: default-deny, namespace: tenant-acme }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
```

**4.3.2 Allow intra-namespace**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: intra-ns-allow, namespace: tenant-acme }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
  ingress:
    - from: [{ podSelector: {} }]
  egress:
    - to: [{ podSelector: {} }]
```

**4.3.3 Allow ingress from soctalk-system (orchestrator MCP calls)**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: allow-from-soctalk-system, namespace: tenant-acme }
spec:
  podSelector:
    matchExpressions:
      - { key: app.kubernetes.io/name, operator: In,
          values: [wazuh-manager, wazuh-indexer, thehive, cortex] }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: soctalk-system }
          podSelector:
            matchLabels: { app.kubernetes.io/name: soctalk-orchestrator }
      ports:
        - { port: 55000, protocol: TCP }
        - { port: 9200,  protocol: TCP }
        - { port: 9000,  protocol: TCP }
        - { port: 9001,  protocol: TCP }
```

**4.3.4 Allow adapter to egress soctalk-system API**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: adapter-egress, namespace: tenant-acme }
spec:
  podSelector: { matchLabels: { app.kubernetes.io/name: soctalk-adapter } }
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: soctalk-system }
          podSelector: { matchLabels: { app.kubernetes.io/name: soctalk-api } }
      ports: [{ port: 8000, protocol: TCP }]
    # DNS
    - to:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: kube-system }
          podSelector: { matchLabels: { k8s-app: kube-dns } }
      ports: [{ port: 53, protocol: UDP }]
```

**4.3.5 Allow Wazuh agent ingress from MSSP edge**

This policy governs agent telemetry arriving at the tenant's Wazuh manager. Since ingress comes from the cluster's node (via SNI proxy / NodePort / ingress controller), the NP needs to permit traffic from the ingress path. Exact allow source depends on MSSP ingress topology (Traefik/nginx/MetalLB IP range). See `wazuh-ingress.md` for the deployment pattern; template:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: wazuh-agent-ingress, namespace: tenant-acme }
spec:
  podSelector: { matchLabels: { app.kubernetes.io/name: wazuh-manager } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: ingress-system }  # or per-MSSP
      ports:
        - { port: 1514, protocol: TCP }
        - { port: 1515, protocol: TCP }
```

## 5 DNS considerations

- Cilium must be configured with `hubble` enabled to observe DNS queries (useful for debugging FQDN policy matches).
- `toFQDNs` policies work by intercepting DNS responses and adding resolved IPs to ephemeral rules. TTL of the DNS response governs policy cache freshness; if an LLM provider has extremely short TTLs (~60s), expect occasional brief connection failures on IP rotation. Mitigation: Cilium's `dnsProxy` can be tuned for longer `minTTL`: set to 300s.
- Corporate DNS (customer-LLM-hosted internally): if the tenant's LLM endpoint resolves only via an internal DNS server, Cilium must be configured to use that server, or the tenant uses IP-based egress (loses FQDN-of-intent semantics).

## 6 Observability

Hubble (bundled with Cilium) is enabled in the reference install. MSSP ops teams can run `hubble observe --namespace tenant-acme` to see flows, enforcement verdicts (allow/deny), and drops. This is the primary debugging tool for tenant isolation questions.

## 7 Testing

A later release gate includes a cross-tenant network isolation test:
1. Deploy two tenants (`tenant-a`, `tenant-b`).
2. From a pod in `tenant-a`, attempt to connect to `tenant-b`'s Wazuh service by IP and by DNS name. Expect connection refused / timeout.
3. From the orchestrator in `soctalk-system`, attempt to call `tenant-a`'s LLM FQDN while operating in `tenant-b` context. Expect application-layer refusal (no key); policy layer may still permit since both FQDNs are in allow-list.
4. From a pod in `soctalk-system` that isn't the orchestrator, attempt to reach `tenant-a`'s Wazuh. Expect connection refused (only orchestrator has egress to tenant data plane ports).

## 8 Deferred (future releases)

- **L7 HTTP policies**: Cilium supports L7 HTTP `CiliumNetworkPolicy` (restrict to specific paths/methods). This release is L4 only. L7 useful for finer MCP call restrictions in a future release.
- **Identity-based policies**: labels-only in this release; Cilium identity with SPIFFE-style mTLS is a future release.
- **Egress gateway for static source IP**: if MSSP end-customers need whitelisted static source IP on SocTalk's LLM calls, Cilium Egress Gateway handles it. a future release.
- **Transparent encryption (WireGuard/IPsec)**: cluster-wide encryption of pod-to-pod traffic. a future release hardening.
