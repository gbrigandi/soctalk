#!/usr/bin/env bash
#
# local-up.sh — opt-in local SocTalk k3d profile.
#
# Default deployment target for SocTalk work is the remote lab
# (kubectl context ``k3d-lab`` on 192.168.1.28). This script gives
# you an isolated on-Mac cluster you can iterate against without
# affecting anyone else.
#
# Different from scripts/dev-up.sh: that one installs Cilium +
# cert-manager and is the full-fidelity dev rig. This one is the
# slim profile — bare k3s + nginx-ingress, nothing else — for fast
# canonical-UI iteration.
#
# Usage:
#   scripts/local-up.sh
#   kubectl get nodes        # .envrc already exports KUBECONFIG=$PWD/.kube/config
#
# Tear down:
#   scripts/local-down.sh
#
# Idempotent — re-run to refresh the kubeconfig if the cluster
# already exists.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

CLUSTER_NAME="${CLUSTER_NAME:-soctalk-local}"
# Project-local kubeconfig. ``.envrc`` exports KUBECONFIG to the same
# path so commands in any direnv-allowed shell talk to this cluster
# without explicit overrides. Older versions of this script wrote to
# ``./kubeconfig.local`` at the repo root; ``.kube/config`` is the new
# canonical location shared with scripts/dev-up.sh.
mkdir -p "${PWD}/.kube"
KCFG="${PWD}/.kube/config"
CONFIG="scripts/k3d-local.yaml"

if k3d cluster list -o json 2>/dev/null | grep -q "\"name\": \"${CLUSTER_NAME}\""; then
  echo "cluster ${CLUSTER_NAME} already exists; refreshing kubeconfig"
  # Make sure it's running (k3d cluster stop preserves state).
  k3d cluster start "${CLUSTER_NAME}" >/dev/null 2>&1 || true
else
  echo "creating cluster ${CLUSTER_NAME} from ${CONFIG}…"
  # ``--kubeconfig-update-default=false --kubeconfig-switch-context=false``
  # keep this profile from clobbering ~/.kube/config or hijacking the
  # current-context. Lab-safe contract: the user only ever opts in by
  # exporting ${KCFG} explicitly.
  k3d cluster create \
    --config "${CONFIG}" \
    --kubeconfig-update-default=false \
    --kubeconfig-switch-context=false
fi

CTX="k3d-${CLUSTER_NAME}"

# Self-contained kubeconfig — CA + client cert/key inlined so the
# file is portable. NOT merged into ~/.kube/config. Write this
# FIRST, then point kubectl/helm at it via KUBECONFIG, otherwise the
# subsequent ``--context`` calls would resolve against ~/.kube/config
# (which doesn't carry our cluster after the round-5 fix to skip
# default-context update).
k3d kubeconfig get "${CLUSTER_NAME}" > "${KCFG}"
chmod 600 "${KCFG}"
export KUBECONFIG="${KCFG}"

# nginx-ingress: canonical install assumes ingressClassName=nginx.
# The k3d-local.yaml port mappings forward 127.0.0.1:8080→loadbalancer:80
# (and 8443→443), so the controller has to publish a Service that
# the loadbalancer can reach. ``hostPort`` on the controller pod
# binds the agent node's :80/:443 directly, which is what the k3d
# loadbalancer's Traefik fork expects when traefik is disabled and
# we're routing through the bundled klipper-lb. Keep the Service as
# a NodePort so klipper-lb has a stable target on every node.
if ! kubectl --context "${CTX}" get ns ingress-system >/dev/null 2>&1; then
  echo "installing ingress-nginx…"
  helm --kube-context "${CTX}" upgrade --install ingress-nginx ingress-nginx \
    --repo https://kubernetes.github.io/ingress-nginx \
    --namespace ingress-system --create-namespace \
    --set controller.ingressClassResource.default=true \
    --set controller.service.type=NodePort \
    --set controller.hostPort.enabled=true \
    --set controller.hostPort.ports.http=80 \
    --set controller.hostPort.ports.https=443 \
    --wait --timeout=5m
fi

cat <<EOF

local profile up
  cluster:    ${CLUSTER_NAME}
  context:    ${CTX}
  kubeconfig: ${KCFG}

  CHART HINT: the canonical soctalk-system chart defaults to
  ``ingress.className: traefik`` and assumes Cilium + cert-manager,
  none of which apply to this nginx-only HTTP profile. Helm-install
  with the full local override set:
    --set ingress.className=nginx \\
    --set ingress.tls.issuerRef='' \\
    --set auth.cookieSecure=false \\
    --set auth.publicOriginOverride=http://<slug>.soctalk.ai:9080 \\
    --set preInstallCheck.enabled=false \\
    --set networkPolicy.cilium=false
  so Ingress routes through nginx, the session cookie isn't dropped
  on plain HTTP, CSRF accepts the port-suffixed origin the browser
  sends, and the Cilium/cert-manager preflight doesn't reject the
  install on a CNI-less local cluster.
  registry:   127.0.0.1:5500   (push: 127.0.0.1:5500/<image>:<tag>)
                              (cluster-side: soctalk-local-registry:5500)
  ingress:    127.0.0.1:9080   (no port-forward needed)

use it:
  export KUBECONFIG=${KCFG}
  kubectl get nodes

stop / destroy:
  k3d cluster stop ${CLUSTER_NAME}    # preserves state
  scripts/local-down.sh               # destroys + removes kubeconfig
EOF
KUBECONFIG="${KCFG}" kubectl get nodes
