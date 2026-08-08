# SocTalk Upgrade Guide

This release supports upgrades via `helm upgrade` for both chart classes. Upgrade and
rollback are **runbook operations** in this release; an API for fleet-wide upgrade
orchestration lands in a future release.

## Pre-flight checklist

Before any upgrade:

1. **Read the release notes** for the target version. Migrations are
   forward-only; a surprise schema change cannot be reverted with
   `helm rollback`.
2. **Verify compatibility matrix**: MSSP UI → System → Versions shows which
   `soctalk-tenant` versions are supported by the target
   `soctalk-system`. Upgrade `soctalk-system` first, then tenants.
3. **Backup** (is MSSP-managed): snapshot Postgres + all tenant PVCs.
   See the [runbook](../runbook/README.md#database-restore-disaster-recovery).
4. **Dry-run** with `helm diff`:
   ```bash
   helm diff upgrade soctalk-system oci://ghcr.io/soctalk/charts/soctalk-system \
     --version <new> -n soctalk-system -f values.yaml
   ```

## Upgrade `soctalk-system` (install-level)

```bash
helm upgrade soctalk-system oci://ghcr.io/soctalk/charts/soctalk-system \
  --version <new-version> \
  --namespace soctalk-system \
  -f soctalk-system-values.yaml \
  --wait --timeout 10m
```

Alembic migrations run on API pod startup, in the `db-init` initContainer.
Monitor:

```bash
kubectl -n soctalk-system logs deploy/soctalk-system-api -f | grep -i alembic
```

The initContainer also seeds the model price catalog. Both steps are
idempotent, so they are safe on every restart, not just on install.

**Concurrency.** Every API replica starts its own `db-init`, so more than one
replica means more than one migration runner. They are serialised by a
Postgres advisory lock: the loser blocks until the winner finishes, then finds
itself already at head. You do **not** need to scale to one replica during an
upgrade. A slow-starting second pod during a migration is expected.

**Do not use `--reuse-values` with a pinned image tag.** If `image.tag` was set
explicitly at install, `--reuse-values` keeps it, and you get the new chart
running the old images. When the database is behind, this appears to work while
silently running old code; when the database is ahead, `db-init` fails outright
with `Can't locate revision identified by ...`. Pass the values file, or add
`--set image.tag=<new-version>` explicitly.

The same trap applies to `tenantProvisioning.*`. Those values pin what a
**newly provisioned** tenant gets, and `--reuse-values` will carry the old ones
forward, so new tenants come up on the previous version while everything else
is current. Verify after upgrading:

```bash
helm get values soctalk-system -n soctalk-system -a \
  | grep -E 'tenantChartVersion|adapterImageTag|runsWorkerImageTag|linuxEpImageTag'
```

### Rollback

```bash
helm rollback soctalk-system <revision> -n soctalk-system --wait
```

**Important**: if the upgrade introduced a migration that touched data,
`helm rollback` will NOT revert the schema. Restore Postgres from the
pre-upgrade backup in addition.

## Upgrade a single tenant's data plane

```bash
helm upgrade tenant-<slug> oci://ghcr.io/soctalk/charts/soctalk-tenant \
  --version <new-tenant-chart-version> \
  --namespace tenant-<slug> \
  -f /tmp/tenant-<slug>-values.yaml \
  --wait --timeout 15m
```

Where `/tmp/tenant-<slug>-values.yaml` is the SocTalk-rendered values file
(retrieve from the SocTalk API or regenerate from tenant config):

```bash
soctalk-cli render-values --tenant <slug> > /tmp/tenant-<slug>-values.yaml
```

### Per-tenant rollback

```bash
helm rollback tenant-<slug> <revision> -n tenant-<slug> --wait
```

Tenant data plane rollbacks are safer than system-level rollbacks: the OSS
stacks (Wazuh/TheHive/Cortex) store their own data in PVCs that `helm
rollback` leaves untouched.

## The upgrade window: tenants keep old behaviour until rolled

Upgrading `soctalk-system` and the database does **not** upgrade existing
per-tenant `runs-worker` pods. Between the system upgrade and each tenant's
roll, that tenant's worker is running the previous release against the new
schema.

This is the riskiest step in an upgrade, and it is silent: nothing in the UI
says a tenant is running old semantics. In 0.2.1 specifically, budget ceilings
and price snapshots are read by the worker from the claim row, so an
un-rolled tenant keeps the previous budget behaviour while the MSSP console
reports the new figures.

Keep the window short, and roll a canary first.

### Canary one tenant

Pick a low-volume tenant, roll it, and confirm it actually triages before
touching the rest:

```bash
NS=tenant-<canary-slug>
helm upgrade $NS oci://ghcr.io/soctalk/charts/soctalk-tenant \
  --version <new> -n $NS -f /tmp/${NS}-values.yaml --wait --timeout 15m

# The worker must be on the new image, not merely restarted.
kubectl -n $NS get deploy soctalk-runs-worker \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

Then verify a real alert completes end to end, rather than assuming a healthy
pod means a working tenant:

```bash
# A run should reach status=completed with non-zero tokens and a stamped
# price_snapshot. A run that stalls at zero tokens means the worker is not
# claiming.
kubectl -n soctalk-system exec soctalk-system-postgres-0 -- \
  psql -U soctalk_admin -d soctalk -At -F'|' -c \
  "SELECT status, tokens_used, round(dollars_used::numeric,6),
          price_snapshot IS NOT NULL
     FROM investigation_runs
    WHERE tenant_id = '<tenant-uuid>'
    ORDER BY started_at DESC LIMIT 1"
```

Only once the canary has completed a run should you roll the remainder.

## Fleet upgrade (manual loop in this release)

```bash
# List tenants.
kubectl get ns -l tenant=true,managed-by=soctalk -o jsonpath='{.items[*].metadata.name}'

# Upgrade each, pausing between.
for ns in tenant-acme tenant-beta tenant-gamma; do
  echo "upgrading $ns..."
  helm upgrade ${ns} oci://ghcr.io/soctalk/charts/soctalk-tenant \
    --version <new> -n $ns -f /tmp/${ns}-values.yaml --wait --timeout 15m
  kubectl -n $ns rollout status deploy/soctalk-adapter
  kubectl -n $ns rollout status deploy/soctalk-runs-worker
  sleep 60  # let heartbeat settle before next.
done
```

a future release replaces this loop with a canary-aware fleet-upgrade API.

## Upgrade ordering

1. Cluster prereqs (CNI, cert-manager, ingress): update independently.
2. `soctalk-system` chart: install-level, run migrations.
3. `soctalk-tenant` for each tenant: one at a time, watching for regressions.

Never upgrade tenant charts ahead of `soctalk-system`: the compatibility
matrix will reject out-of-range combinations, and the API will refuse to
provision new tenants on mismatched versions.

## Breaking-change tenant chart upgrades

If the tenant chart bumps a Wazuh/TheHive/Cortex major version with schema
change:

1. Snapshot tenant PVCs first.
2. Upgrade in low-traffic window.
3. Verify alerts flow end-to-end immediately after.
4. Be prepared to `helm rollback` + restore PVCs if the data plane's
   schema-migration process fails.

Upstream OSS projects occasionally ship breaking changes. The chart
audit (`docs/multi-tenant/chart-audit.md`) pins exact subchart versions; bumping
those versions is explicit and tested before release.
