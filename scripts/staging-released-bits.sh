#!/usr/bin/env bash
# Put the staging cluster on released bits, and prove it.
#
#   STAGING_APISERVER=https://<host>:6443 scripts/staging-released-bits.sh <version> [--apply]
#
# Without --apply this only reports: what the box runs now, what the registry
# publishes for <version>, and whether they agree.
#
# --apply reinstalls ONLY the soctalk-system release from the published OCI
# chart. Tenant namespaces are VERIFIED, never upgraded: tenant releases are
# owned by the provisioning controller, and the tenant charts also default to
# pullPolicy: IfNotPresent, so a tenant sitting on a cached image of a
# republished tag must be reconciled through the controller (or have its
# release upgraded and its pods restarted) before it will move. This script
# will fail the run until it does — it does not fix it for you.
#
# Why this exists
# ---------------
# Two things make "the staging cluster runs the release" harder to guarantee
# than it looks:
#
#   1. The box has been running short-sha builds (ghcr.io/soctalk/soctalk-api
#      :3332bfc), not release tags, so a gate run proved nothing about the
#      release.
#
#   2. Every chart in the repo sets `pullPolicy: IfNotPresent`, and a release
#      may republish an EXISTING version tag with different content. k3s then
#      keeps serving the cached layers, so the deploy "succeeds" against stale
#      bits and every test passes for the wrong reason. A tag is not evidence.
#
# So the check is by DIGEST: each running container's imageID must equal the
# digest the registry currently serves for that tag. That is the only statement
# that survives a mutable tag.
#
# Note the digest is read from the registry at verification time, so this
# proves "the box runs what the registry serves NOW". If a later push moves the
# tag, the box keeps running what it pulled — re-run to confirm.
set -uo pipefail

VERSION="${1:?usage: STAGING_APISERVER=https://<host>:6443 staging-released-bits.sh <version> [--apply]}"
APPLY="${2:-}"

# Point at the staging apiserver. Export STAGING_APISERVER to target it directly
# over the operator overlay; leave it unset to use the current kubectl context.
APISERVER="${STAGING_APISERVER:-}"
if [[ -n "$APISERVER" ]]; then
  KUBECTL=(kubectl --server="$APISERVER" --insecure-skip-tls-verify)
  HELM=(helm --kube-apiserver="$APISERVER" --kube-insecure-skip-tls-verify)
else
  KUBECTL=(kubectl)
  HELM=(helm)
fi
REGISTRY="ghcr.io/soctalk"

# Preflight: a gate that cannot reach the cluster must FAIL, not pass. Without
# this, an unreachable/unauthorized kubectl prints its errors to stderr (which
# the per-namespace query discards), the check loop iterates over zero pods,
# nothing is ever marked failed, and the script cheerfully reports that every
# container runs the published digest — the exact false pass it exists to
# prevent. Probe once, loudly.
if ! "${KUBECTL[@]}" get ns soctalk-system >/dev/null 2>&1; then
  echo "cannot reach the cluster (or read namespace soctalk-system) with:" >&2
  echo "  ${KUBECTL[*]}" >&2
  echo "Set STAGING_APISERVER and/or KUBECONFIG so this resolves, then re-run." >&2
  echo "Refusing to report a result from a cluster this script cannot see." >&2
  exit 2
fi

# soctalk-api and soctalk-app-ui run in the system namespace; the orchestrator,
# adapter, and (when the tenant renders the endpoint component) linux-ep run per
# tenant. linux-ep is optional per tenant, so its published digest is resolved
# up front but only checked where a linux-ep pod actually runs — a tenant that
# doesn't render it simply has no such pod, and never trips the unknown-image
# fail path below.
SYSTEM_IMAGES="soctalk-api soctalk-app-ui"
TENANT_IMAGES="soctalk-orchestrator soctalk-adapter soctalk-linux-ep"

published_digest() {
  # `--format` is not honoured by every docker/buildx build, and when it is
  # ignored the whole descriptor block comes back and silently poisons the
  # comparison — so parse the Digest line instead of trusting the flag.
  docker buildx imagetools inspect "$REGISTRY/$1:$2" 2>/dev/null \
    | awk '/^Digest:/ { print $2; exit }'
}

# macOS ships bash 3.2, which has no associative arrays: keep the published
# digests in a temp file and look them up by name.
WORK="$(mktemp -d)"
DIGESTS="$WORK/digests"
FAILED="$WORK/failed"
# Counts containers actually examined. check_ns runs its loop in a pipeline
# subshell, so this has to be a file, not a variable.
CHECKED="$WORK/checked"

echo "== what the registry publishes for $VERSION =="
for img in $SYSTEM_IMAGES $TENANT_IMAGES; do
  d="$(published_digest "$img" "$VERSION")"
  printf '%s %s\n' "$img" "$d" >> "$DIGESTS"
  printf '  %-24s %s\n' "$img:$VERSION" "${d:-<unavailable>}"
  [ -n "$d" ] || { echo "  cannot resolve $img:$VERSION — is the cut finished?"; exit 1; }
done

want_for() { awk -v n="$1" '$1==n {print $2}' "$DIGESTS"; }

if [ "$APPLY" = "--apply" ]; then
  echo
  echo "== installing the published chart (oci://$REGISTRY/charts/soctalk-system:$VERSION) =="
  # pullPolicy=Always because the version tag is mutable: without it a cached
  # image of the same tag is reused and the gate tests stale bits.
  "${HELM[@]}" upgrade --install soctalk-system \
    "oci://$REGISTRY/charts/soctalk-system" --version "$VERSION" \
    --namespace soctalk-system --reuse-values \
    --set image.registry="$REGISTRY" \
    --set image.tag="$VERSION" \
    --set image.pullPolicy=Always \
    --wait --timeout 10m || { echo "helm upgrade failed"; exit 1; }

  # --wait returns when the new ReplicaSet is Ready, but a rollout that changed
  # nothing in the pod spec would not restart pods at all — force it so the
  # Always policy actually re-pulls.
  "${KUBECTL[@]}" -n soctalk-system rollout restart deploy/soctalk-system-api deploy/soctalk-system-app-ui
  "${KUBECTL[@]}" -n soctalk-system rollout status deploy/soctalk-system-api --timeout=5m
  "${KUBECTL[@]}" -n soctalk-system rollout status deploy/soctalk-system-app-ui --timeout=5m
fi

echo
echo "== what the staging cluster actually runs =="
rc=0
# Returns nonzero when the pod read itself failed. Callers MUST check: a failed
# read produces no output, which is indistinguishable from "namespace is clean"
# unless the status is honoured. `set -o pipefail` (top of file) makes the
# pipeline carry kubectl's failure through.
check_ns() {
  local ns="$1"
  "${KUBECTL[@]}" get pods -n "$ns" \
    -o jsonpath='{range .items[*].status.containerStatuses[*]}{.image}{" "}{.imageID}{"\n"}{end}' \
  | while read -r image imageid; do
      [ -n "$image" ] || continue
      case "$image" in
        */soctalk-*) ;;
        *) continue ;;
      esac
      short="${image##*/}"; name="${short%%:*}"; tag="${short##*:}"
      echo x >> "$CHECKED"
      want="$(want_for "$name")"
      got="${imageid##*@}"
      if [ -z "$want" ]; then
        # A soctalk image this script does not know about is NOT a pass: it is
        # an unverified container, and reporting it as a harmless '?' is the
        # same false pass the digest check exists to prevent.
        printf '  X  %-46s unverified: no published digest known\n' "$short"
        echo FAIL >> "$FAILED"
      elif [ "$tag" != "$VERSION" ]; then
        printf '  X  %-46s runs tag %s, not %s\n' "$name" "$tag" "$VERSION"
        echo FAIL >> "$FAILED"
      elif [ "$got" = "$want" ]; then
        printf '  ok %-46s %s\n' "$short" "${got:0:19}..."
      else
        printf '  X  %-46s digest mismatch\n' "$short"
        printf '       running   %s\n       published %s\n' "$got" "$want"
        echo FAIL >> "$FAILED"
      fi
    done
}
check_ns soctalk-system || {
  echo "reading pods in soctalk-system FAILED — cannot verify this namespace." >&2
  echo "Refusing to report success from an incomplete inspection." >&2
  exit 2
}

# Enumerate tenant namespaces with an explicitly checked call. A failed list
# yields no output, which would otherwise look like "no tenants exist" and let
# the run pass having verified only soctalk-system.
if ! ALL_NS="$("${KUBECTL[@]}" get ns -o name)"; then
  echo "listing namespaces FAILED — cannot enumerate tenant namespaces." >&2
  echo "Refusing to report success without inspecting them." >&2
  exit 2
fi
for ns in $(printf '%s\n' "$ALL_NS" | sed 's|namespace/||' | grep '^tenant-'); do
  echo "  -- $ns --"
  check_ns "$ns" || {
    echo "reading pods in $ns FAILED — cannot verify this namespace." >&2
    echo "Refusing to report success from an incomplete inspection." >&2
    exit 2
  }
done

# Zero containers examined is not a pass. It means the cluster has no soctalk
# workloads, or the query silently returned nothing — either way this run proves
# nothing and must not print a success line.
if [ ! -s "$CHECKED" ]; then
  echo
  echo "checked 0 soctalk containers — nothing was verified."
  echo "Expected soctalk pods in soctalk-system (and any tenant-* namespaces)."
  echo "Refusing to report success from a run that inspected nothing."
  exit 2
fi

if [ -s "$FAILED" ]; then
  echo
  echo "NOT on released bits."
  [ "$APPLY" = "--apply" ] || echo "Re-run with --apply to install the published artifacts."
  exit 1
fi
echo
echo "Every soctalk container runs the digest the registry publishes for $VERSION."
