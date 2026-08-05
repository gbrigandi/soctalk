#!/usr/bin/env bash
# SocTalk one-command installer for Linux (amd64).
#
#   curl -sfL https://raw.githubusercontent.com/soctalk/soctalk/<version>/install.sh | bash
#
# Installs k3s + Helm (if missing) and the published soctalk-system chart
# from GHCR, then prints the URL. This is the SAME install core the VM
# appliance runs at first boot (infra/packer/scripts/firstboot.sh sources
# this file); the only difference is the chart source — the appliance
# passes a bundled chart directory, this path uses the OCI chart.
#
# Modes:
#   --demo            non-interactive; random admin password; onboards a
#                     demo tenant. The fastest "just show me" path.
#   (default)         prompts for MSSP name, admin email/password,
#                     hostname, and LLM provider/key — or reads them from
#                     SOCTALK_* env vars for unattended real installs.
#
# Run with --help for the full flag/env reference.
set -euo pipefail

# k3s / helm install their binaries (and the `kubectl` symlink) into
# /usr/local/bin. That directory is NOT on the sudo secure_path on RHEL-family
# distros, so when this installer runs via `sudo soctalk install`, bare
# `kubectl`/`helm`/`k3s` calls 404 — which made the k3s readiness probe below
# fail for its whole window and wrongly report "k3s did not become ready".
# Put it on PATH up front so every downstream tool call resolves.
case ":$PATH:" in *":/usr/local/bin:"*) ;; *) export PATH="/usr/local/bin:$PATH" ;; esac

# --------------------------------------------------------------------- #
# Defaults (override via flags or SOCTALK_* env). CHART_VERSION tracks
# the release this installer shipped with; pin the installer by fetching
# it from a release tag (…/soctalk/<version>/install.sh).
# --------------------------------------------------------------------- #
CHART_REF="${SOCTALK_CHART_REF:-oci://ghcr.io/soctalk/charts/soctalk-system}"
CHART_VERSION="${SOCTALK_CHART_VERSION:-0.2.0}"
# Pin images to the chart version by default so a release-tagged installer
# deploys a matching image set, not whatever 'latest' has moved to. The
# publish workflow tags images with the release version (e.g. 0.1.2).
IMAGE_TAG="${SOCTALK_IMAGE_TAG:-$CHART_VERSION}"
CHART_DIR=""                       # set by --chart-dir (appliance path)
NAMESPACE="soctalk-system"
HELM_TIMEOUT="${SOCTALK_HELM_TIMEOUT:-15m}"

MODE="interactive"                 # interactive | demo | values-file
VALUES_FILE=""                     # --values-file (appliance/unattended)
LLM_KEY_FILE=""                    # --llm-key-file (appliance)
ONBOARD_ENV=""                     # --onboard-env (appliance)
ONBOARD_DEMO="false"
SKIP_PREFLIGHT="false"
SKIP_CONSENT="false"
ASSUME_YES="${SOCTALK_ASSUME_YES:-false}"

# Config (env-overridable; prompted in interactive mode when unset).
MSSP_NAME="${SOCTALK_MSSP_NAME:-}"
ADMIN_EMAIL="${SOCTALK_ADMIN_EMAIL:-}"
ADMIN_PASSWORD="${SOCTALK_ADMIN_PASSWORD:-}"
HOSTNAME_IN="${SOCTALK_HOSTNAME:-}"
LLM_PROVIDER="${SOCTALK_LLM_PROVIDER:-anthropic}"
LLM_API_KEY="${SOCTALK_LLM_API_KEY:-}"

KUBECONFIG_PATH="/etc/rancher/k3s/k3s.yaml"

# SocTalk runs on k3s (systemd) everywhere: Linux servers, the VM appliance,
# a VPS, and Windows-via-WSL2 (k3s running inside the WSL2 distro, exposed to
# the Windows host through WSL2's localhost forwarding). The API is reached
# through Traefik on 443 with the ingress Host header.
API_BASE="https://127.0.0.1"

# --------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------- #
# Colour only when stdout is a real terminal. When the installer is piped
# (e.g. run over SSH by launchpad and streamed into the web console) raw ANSI
# codes would otherwise surface as literal [32m ... [0m in the event log.
if [[ -t 1 ]]; then
  c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_rst=$'\033[0m'
else
  c_bold=''; c_red=''; c_grn=''; c_yel=''; c_rst=''
fi
log()  { printf '%s==>%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%sWARN:%s %s\n' "$c_yel" "$c_rst" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$c_red" "$c_rst" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
SocTalk one-command installer for Linux (amd64).

  curl -sfL https://raw.githubusercontent.com/soctalk/soctalk/<version>/install.sh | bash

Installs k3s + Helm (if missing) and the published soctalk-system chart from
GHCR, then prints the URL. This is the same install core the VM appliance runs
at first boot; only the chart source differs.

  --demo     non-interactive demo install (random admin pw, demo tenant)
  (default)  prompts for config, or reads it from SOCTALK_* env vars

Flags:
  --demo                  non-interactive demo install (random admin pw, demo tenant)
  --chart-version <v>     OCI chart version (default tracks this installer)
  --chart-dir <path>      install from a local chart dir instead of OCI (appliance)
  --values-file <path>    use a pre-rendered values.yaml (appliance/unattended)
  --llm-key-file <path>   read the LLM key from a file instead of env/prompt
  --onboard-env <path>    onboard a tenant from a sourced env file (appliance)
  --onboard-demo          onboard a "demo" tenant after install
  --skip-preflight        skip the host preflight checks
  --skip-consent          don't pause for confirmation before mutating the host
  -y, --yes               assume yes to the consent prompt
  -h, --help              this help

Env: SOCTALK_MSSP_NAME, SOCTALK_ADMIN_EMAIL, SOCTALK_ADMIN_PASSWORD,
     SOCTALK_HOSTNAME, SOCTALK_LLM_PROVIDER, SOCTALK_LLM_API_KEY,
     SOCTALK_CHART_REF, SOCTALK_CHART_VERSION, SOCTALK_HELM_TIMEOUT,
     SOCTALK_ASSUME_YES (auto-set when all three required vars above
     are present, so curl|bash unattended flows don't need -y)
EOF
}

uuid() { cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen; }

# Escape an arbitrary value for a single-quoted YAML scalar (fully
# literal; only embedded ' needs doubling). Safe for names/passwords.
yaml_sq() { printf "'%s'" "${1//\'/\'\'}"; }

# Escape an arbitrary value into a JSON string literal (handles backslash,
# quote, and common control chars). Used for the onboard request bodies.
json_str() {
  local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
  printf '"%s"' "$s"
}

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      die "must run as root. Re-run with: curl -sfL <url> | sudo bash"
    fi
    die "must run as root."
  fi
}

# --------------------------------------------------------------------- #
# Preflight — light, Linux-only. The VM appliance covers everyone else.
# --------------------------------------------------------------------- #
# SocTalk's installer manages k3s as a systemd service and waits on the
# systemd-written kubeconfig. On a non-systemd host (Alpine/OpenRC and other
# init systems) that path can't work, so fail fast with a clear pointer rather
# than dying later on a cryptic BusyBox/df error or hanging on the kubeconfig
# wait. `/run/systemd/system` exists only when PID 1 is systemd.
require_systemd() {
  [[ -d /run/systemd/system ]] && return 0
  die "soctalk install requires a systemd-based host (it runs k3s as a systemd service). This host is not running systemd (e.g. Alpine/OpenRC). Use a systemd distro (Debian/Ubuntu, RHEL/Fedora/Rocky/Alma) or the prebuilt VM appliance: https://soctalk.github.io/soctalk-docs/downloads"
}

preflight() {
  log "Preflight checks"
  local fail=0

  [[ "$(uname -s)" == "Linux" ]] || die "this installer is Linux-only. On Windows/macOS use the VM appliance: https://soctalk.github.io/soctalk-docs/downloads"

  local arch; arch="$(uname -m)"
  if [[ "$arch" != "x86_64" && "$arch" != "amd64" ]]; then
    die "unsupported architecture '$arch'. SocTalk images are amd64-only. On arm64 (incl. Apple Silicon) use a Linux amd64 VPS or the cloud demo."
  fi
  printf '  %-22s %s\n' "architecture" "${c_grn}ok${c_rst} ($arch)"

  local mem_kb mem_gb
  mem_kb="$(awk '/MemTotal/{print $2}' /proc/meminfo)"
  mem_gb=$(( mem_kb / 1024 / 1024 ))
  if (( mem_gb < 7 )); then
    printf '  %-22s %s\n' "memory" "${c_red}low${c_rst} (${mem_gb}Gi; 8Gi recommended)"; fail=1
  else
    printf '  %-22s %s\n' "memory" "${c_grn}ok${c_rst} (${mem_gb}Gi)"
  fi

  local disk_gb
  # POSIX `df -Pk` (portable to BusyBox and GNU) reports available KiB in
  # column 4; convert to GiB. Avoid `df -BG --output=avail` — those are GNU
  # coreutils-only options that abort under `set -o pipefail` on BusyBox df.
  disk_gb="$(df -Pk / 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}')"
  if [[ -n "$disk_gb" ]] && (( disk_gb < 20 )); then
    printf '  %-22s %s\n' "disk (/)" "${c_red}low${c_rst} (${disk_gb}Gi; 20Gi+ recommended)"; fail=1
  else
    printf '  %-22s %s\n' "disk (/)" "${c_grn}ok${c_rst} (${disk_gb:-?}Gi free)"
  fi

  local busy="" p
  if ! command -v k3s >/dev/null 2>&1; then
    # Ports only matter if k3s/Traefik isn't already here. Prefer ss, fall back
    # to netstat; with neither we can't tell, so say so rather than claim "free"
    # (a false all-clear would let the install march into a port conflict).
    local port_tool=""
    command -v ss >/dev/null 2>&1 && port_tool="ss"
    [[ -z "$port_tool" ]] && command -v netstat >/dev/null 2>&1 && port_tool="netstat"
    if [[ -z "$port_tool" ]]; then
      printf '  %-22s %s\n' "ports 80/443/6443" "${c_ylw:-}unknown${c_rst} (no ss/netstat)"
    else
      for p in 80 443 6443; do
        if [[ "$port_tool" == "ss" ]]; then
          ss -ltn "( sport = :$p )" 2>/dev/null | grep -q ":$p" && busy="$busy $p"
        else
          netstat -ltn 2>/dev/null | grep -qE "[:.]${p}[[:space:]]" && busy="$busy $p"
        fi
      done
      if [[ -n "$busy" ]]; then printf '  %-22s %s\n' "ports 80/443/6443" "${c_red}in use:${c_rst}$busy"; fail=1
      else printf '  %-22s %s\n' "ports 80/443/6443" "${c_grn}free${c_rst}"; fi
    fi
  fi

  # Reachability: a *connection* (any HTTP response) is success. Omit -f so
  # ghcr.io's 401 at /v2/ still counts as reachable; curl returns non-zero
  # only on DNS/connect/TLS/timeout failure — which is what we want to catch.
  local url urls=("https://get.k3s.io" "https://ghcr.io/v2/")
  for url in "${urls[@]}"; do
    if curl -s -m 8 -o /dev/null "$url" 2>/dev/null; then
      printf '  %-22s %s\n' "reach ${url#https://}" "${c_grn}ok${c_rst}"
    else
      printf '  %-22s %s\n' "reach ${url#https://}" "${c_red}unreachable${c_rst}"; fail=1
    fi
  done

  if (( fail )); then
    warn "preflight found issues above."
    [[ "$ASSUME_YES" == "true" ]] || { printf 'Continue anyway? [y/N] '; read -r a < /dev/tty || a=""; [[ "$a" =~ ^[Yy]$ ]] || die "aborted by preflight."; }
  fi
}

# --------------------------------------------------------------------- #
# Consent — be explicit about host mutation before touching anything.
# --------------------------------------------------------------------- #
confirm_changes() {
  [[ "$SKIP_CONSENT" == "true" ]] && return 0
  cat <<EOF

${c_bold}SocTalk will make these changes to this host:${c_rst}
  - install k3s (a lightweight Kubernetes) as a systemd service, if missing
    (this configures containerd, a CNI, and iptables rules)
  - install the Helm CLI, if missing
  - create Kubernetes namespace '$NAMESPACE' and a SocTalk install in it
  - bind ports 80/443 (ingress) and 6443 (Kubernetes API)

Uninstall later with:  /usr/local/bin/k3s-uninstall.sh

EOF
  [[ "$ASSUME_YES" == "true" ]] && { log "Proceeding (--yes)"; return 0; }
  printf 'Proceed? [y/N] '
  local a; read -r a < /dev/tty || a=""
  [[ "$a" =~ ^[Yy]$ ]] || die "aborted."
}

# --------------------------------------------------------------------- #
# Runtime: k3s + Helm + kubeconfig
# --------------------------------------------------------------------- #
ensure_k3s() {
  # Provision registries.yaml BEFORE k3s installs so containerd is born
  # knowing about the lab OCI mirror. Off unless SOCTALK_LAB_REGISTRY is
  # set (e.g. 100.102.223.8:5000). Public/production installs pull from
  # ghcr.io over HTTPS and don't need this.
  if [[ -n "${SOCTALK_LAB_REGISTRY:-}" ]]; then
    mkdir -p /etc/rancher/k3s
    cat > /etc/rancher/k3s/registries.yaml <<REG
mirrors:
  "${SOCTALK_LAB_REGISTRY}":
    endpoint:
      - "http://${SOCTALK_LAB_REGISTRY}"
configs:
  "${SOCTALK_LAB_REGISTRY}":
    tls:
      insecure_skip_verify: true
REG
    log "wrote /etc/rancher/k3s/registries.yaml for ${SOCTALK_LAB_REGISTRY}"
  fi
  if command -v k3s >/dev/null 2>&1; then
    log "k3s already present — starting it"
    systemctl start k3s 2>/dev/null || true
  else
    log "Installing k3s"
    curl -sfL https://get.k3s.io | sh -
  fi
  # Bounded wait for the kubeconfig: if k3s failed to start (e.g. the service
  # never came up), don't hang forever — surface it. ~2 min at 1s intervals.
  local kc
  for kc in $(seq 1 120); do [[ -f "$KUBECONFIG_PATH" ]] && break; sleep 1; done
  [[ -f "$KUBECONFIG_PATH" ]] || die "k3s did not produce $KUBECONFIG_PATH within 2 minutes — check 'systemctl status k3s' and 'journalctl -u k3s'."
  export KUBECONFIG="$KUBECONFIG_PATH"
  chmod 600 "$KUBECONFIG_PATH" 2>/dev/null || true
  # Up to 5 min: a first k3s boot pulls its system images + inits containerd,
  # which is slower on constrained / first-boot hosts (120s was too tight).
  local i
  for i in $(seq 1 150); do kubectl get nodes >/dev/null 2>&1 && break; sleep 2; done
  kubectl get nodes >/dev/null 2>&1 || die "k3s did not become ready"
}

ensure_helm() {
  if command -v helm >/dev/null 2>&1; then return 0; fi
  log "Installing Helm"
  curl -sSfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
}

# --------------------------------------------------------------------- #
# Config + values rendering
# --------------------------------------------------------------------- #
prompt_config() {
  [[ "$MODE" == "demo" ]] && {
    MSSP_NAME="${MSSP_NAME:-Demo MSSP}"
    ADMIN_EMAIL="${ADMIN_EMAIL:-admin@soctalk.local}"
    ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(uuid | tr -d - | cut -c1-16)}"
    LLM_API_KEY="${LLM_API_KEY:-sk-REPLACE-ME}"
    # --demo still seeds a demo tenant for the standalone "just show me" path,
    # but callers that only want the non-interactive install (e.g. launchpad,
    # which onboards its own real tenants) can suppress it with
    # SOCTALK_ONBOARD_DEMO=false so no throwaway 'demo' tenant is created.
    ONBOARD_DEMO="${SOCTALK_ONBOARD_DEMO:-true}"
    return 0
  }
  # interactive: only prompt for what env didn't supply, and only if a tty exists
  if [[ -t 0 || -e /dev/tty ]]; then
    [[ -n "$MSSP_NAME" ]]     || { printf 'MSSP / organization name: '; read -r MSSP_NAME < /dev/tty; }
    [[ -n "$ADMIN_EMAIL" ]]   || { printf 'Admin email: '; read -r ADMIN_EMAIL < /dev/tty; }
    [[ -n "$ADMIN_PASSWORD" ]]|| { printf 'Admin password (min 12 chars): '; read -rs ADMIN_PASSWORD < /dev/tty; echo; }
    [[ -n "$HOSTNAME_IN" ]]   || { printf 'Hostname (blank = soctalk.local): '; read -r HOSTNAME_IN < /dev/tty; }
    [[ -n "$LLM_API_KEY" || -n "$LLM_KEY_FILE" ]] || { printf 'LLM API key (%s): ' "$LLM_PROVIDER"; read -rs LLM_API_KEY < /dev/tty; echo; }
  fi
  [[ -n "$MSSP_NAME" ]]      || die "MSSP name required (set SOCTALK_MSSP_NAME or use --demo)"
  [[ -n "$ADMIN_EMAIL" ]]    || die "admin email required (set SOCTALK_ADMIN_EMAIL)"
  [[ -n "$ADMIN_PASSWORD" ]] || die "admin password required (set SOCTALK_ADMIN_PASSWORD)"
}

# Env-driven unattended path: when the caller has supplied every
# required SOCTALK_* var, they're plainly running unattended (CI,
# cloud-init, ansible). Auto-assume yes so the install doesn't block
# on the /dev/tty prompt that no terminal exists to answer. Explicit
# --yes / SOCTALK_ASSUME_YES still wins for the partial cases.
#
# MUST run before preflight/confirm_changes, not inside prompt_config:
# both consent prompts fire earlier in main(), and a no-TTY run used to
# die there with "/dev/tty: No such device or address" even though every
# required env var was present (issue #108).
auto_assume_yes_from_env() {
  # ${VAR:-} guards: the script runs under ``set -u`` and an interactive
  # install has none of these exported — a bare reference would abort.
  if [[ "$ASSUME_YES" != "true" \
        && -n "${SOCTALK_MSSP_NAME:-}" \
        && -n "${SOCTALK_ADMIN_EMAIL:-}" \
        && -n "${SOCTALK_ADMIN_PASSWORD:-}" ]]; then
    ASSUME_YES="true"
  fi
}

render_values() {
  local host="${HOSTNAME_IN:-soctalk.local}"
  local mssp_q email_q pw_q host_q
  mssp_q=$(yaml_sq "$MSSP_NAME")
  email_q=$(yaml_sq "$ADMIN_EMAIL")
  pw_q=$(yaml_sq "$ADMIN_PASSWORD")
  host_q=$(yaml_sq "$host")
  # Persist install/mssp UUIDs across runs so re-running the installer
  # is idempotent — the api pod's db-init upserts on install_id, and
  # organizations.slug has a unique constraint that would collide if we
  # generated a fresh install_id for the same MSSP on each run.
  local id_file="/etc/soctalk/install-ids"
  local mssp_id install_id
  if [[ -r "$id_file" ]]; then
    # shellcheck disable=SC1090
    . "$id_file"
    mssp_id="${SOCTALK_MSSP_ID:-$(uuid)}"
    install_id="${SOCTALK_INSTALL_ID:-$(uuid)}"
  else
    mssp_id="$(uuid)"
    install_id="$(uuid)"
    mkdir -p "$(dirname "$id_file")"
    printf 'SOCTALK_MSSP_ID=%s\nSOCTALK_INSTALL_ID=%s\n' "$mssp_id" "$install_id" > "$id_file"
    chmod 600 "$id_file"
  fi
  # BusyBox mktemp rejects a suffix after XXXXXX (it must be the trailing
  # chars), so don't use ".XXXXXX.yaml". Make the temp file, then give it the
  # .yaml name k3s/helm expect.
  VALUES_FILE="$(mktemp /tmp/soctalk-values.XXXXXX)" && mv "$VALUES_FILE" "$VALUES_FILE.yaml" && VALUES_FILE="$VALUES_FILE.yaml"
  cat > "$VALUES_FILE" <<EOF
# Generated by install.sh at $(date -u +%FT%TZ). Do not hand-edit.
install:
  msspId: "$mssp_id"
  msspName: $mssp_q
  installId: "$install_id"
  installLabel: "soctalk"
  bootstrapAdmin:
    email: $email_q
    password: $pw_q
image:
  registry: ${SOCTALK_IMAGE_REGISTRY:-ghcr.io/soctalk}
  tag: "$IMAGE_TAG"
ingress:
  enabled: true
  className: traefik
  # k3s ships Traefik in kube-system; the chart's default 'ingress-system'
  # matches a dedicated-controller install. Setting this here means the
  # NetworkPolicy that gates the api + app-ui services allows kube-system
  # to reach them from the start — no post-install kubectl patch needed
  # (and helm upgrade doesn't clobber the manual patch either).
  controllerNamespace: kube-system
  tls:
    issuerRef: ""
    secretName: soctalk-tls
  hostnames:
    mssp: $host_q
    customer: $host_q
defaults:
  llm:
    provider: $(yaml_sq "$LLM_PROVIDER")
tenantProvisioning:
  # Explicit null-map guard: helm merges an empty tenantProvisioning block
  # (which happens when none of the SOCTALK_TENANT_* env vars below are set)
  # into a nil map, and the chart's 30-api.yaml then fails with a nil-pointer
  # on .Values.tenantProvisioning.adapterImageRepo. Emitting an explicit
  # chart-default entry below keeps the map non-nil so the chart's other
  # tenantProvisioning defaults survive the merge.
  # NOTE: this heredoc is unquoted, so it expands shell variables. Keep
  # backticks and command substitution out of these comments, or bash will
  # try to expand or run them (and set -u aborts on any unbound name).
  helmPlainHttp: false
EOF
  # Optional tenant chart pin — useful for lab registries / staged
  # publishing where SOCTALK_TENANT_CHART_REF points at a private OCI
  # mirror (oci://lab.example:5000/charts/soctalk-tenant) and a specific
  # version is being validated. Falls through to the chart's defaults
  # otherwise (public ghcr.io/soctalk).
  if [[ -n "${SOCTALK_TENANT_CHART_REF:-}" ]]; then
    printf '  tenantChartRef: %s\n' "$(yaml_sq "$SOCTALK_TENANT_CHART_REF")" >> "$VALUES_FILE"
  fi
  if [[ -n "${SOCTALK_TENANT_CHART_VERSION:-}" ]]; then
    printf '  tenantChartVersion: %s\n' "$(yaml_sq "$SOCTALK_TENANT_CHART_VERSION")" >> "$VALUES_FILE"
  fi
  if [[ -n "${SOCTALK_AGENT_CHART_REF:-}" ]]; then
    printf '  agentChartRef: %s\n' "$(yaml_sq "$SOCTALK_AGENT_CHART_REF")" >> "$VALUES_FILE"
  fi
  if [[ -n "${SOCTALK_AGENT_CHART_VERSION:-}" ]]; then
    printf '  agentChartVersion: %s\n' "$(yaml_sq "$SOCTALK_AGENT_CHART_VERSION")" >> "$VALUES_FILE"
  fi
  # SOCTALK_HELM_PLAIN_HTTP already toggles --plain-http on THIS installer's
  # helm invocation; propagate the same intent to the api pod so its sync
  # helm SDK (used for tenant provisioning) speaks plain HTTP to the same
  # lab OCI registry.
  if [[ "${SOCTALK_HELM_PLAIN_HTTP:-}" == "1" || "${SOCTALK_HELM_PLAIN_HTTP:-}" == "true" ]]; then
    printf '  helmPlainHttp: true\n' >> "$VALUES_FILE"
  fi
  # Pod-level /etc/hosts entries baked into every new tenant install so
  # cross-cluster tenants can reach the MSSP hostname when their DNS
  # can't (Tailscale MagicDNS off, on-prem split-horizon). Semicolon-list
  # of ``ip=host`` pairs.
  if [[ -n "${SOCTALK_L1_HOST_ALIASES:-}" ]]; then
    printf '  l1HostAliases: %s\n' "$(yaml_sq "$SOCTALK_L1_HOST_ALIASES")" >> "$VALUES_FILE"
  fi
  # TLS verification for the tenant adapter's connection back to this MSSP.
  # Set SOCTALK_L1_VERIFY_SSL=false when the MSSP serves a self-signed cert
  # (launchpad demo / pending launchpad-owned certs) so tenant adapters can
  # heartbeat + forward alerts. Threaded to the api pod, which emits it into
  # each tenant install's soctalkSystem.verifySsl.
  if [[ -n "${SOCTALK_L1_VERIFY_SSL:-}" ]]; then
    printf '  l1VerifySsl: %s\n' "$(yaml_sq "$SOCTALK_L1_VERIFY_SSL")" >> "$VALUES_FILE"
  fi
  cat >> "$VALUES_FILE" <<EOF
postgres:
  enabled: true
  storage:
    size: 20Gi
preInstallCheck:
  enabled: false
EOF
}

create_llm_secret() {
  local keysrc
  if [[ -n "$LLM_KEY_FILE" ]]; then
    keysrc="$LLM_KEY_FILE"
  else
    keysrc="$(mktemp /tmp/soctalk-llm.XXXXXX)"; chmod 600 "$keysrc"
    printf '%s' "$LLM_API_KEY" > "$keysrc"
  fi
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
  # Materialize both provider keys from the one file (the chart picks by
  # provider env default) — mirrors the appliance first-boot behavior.
  kubectl -n "$NAMESPACE" create secret generic soctalk-system-llm-api-key \
    --from-file=anthropic-api-key="$keysrc" \
    --from-file=openai-api-key="$keysrc" \
    --dry-run=client -o yaml | kubectl apply -f -
  [[ -n "$LLM_KEY_FILE" ]] || rm -f "$keysrc"
}

install_chart() {
  log "Installing soctalk-system (this pulls images; first run takes a few minutes)"
  # Lab / staging OCI registries often serve HTTP (zot, docker registry:2).
  # Pass --plain-http through so operators pointing SOCTALK_CHART_REF at a
  # private mirror don't have to hand-edit the helm invocation.
  local plain_http=()
  [[ "${SOCTALK_HELM_PLAIN_HTTP:-}" == "1" || "${SOCTALK_HELM_PLAIN_HTTP:-}" == "true" ]] \
    && plain_http=(--plain-http)
  if [[ -n "$CHART_DIR" ]]; then
    helm upgrade --install soctalk-system "$CHART_DIR" \
      --namespace "$NAMESPACE" --create-namespace \
      --values "$VALUES_FILE" --wait --timeout "$HELM_TIMEOUT"
  else
    helm upgrade --install soctalk-system "$CHART_REF" --version "$CHART_VERSION" \
      "${plain_http[@]}" \
      --namespace "$NAMESPACE" --create-namespace \
      --values "$VALUES_FILE" --wait --timeout "$HELM_TIMEOUT"
  fi
}

patch_networkpolicy() {
  # k3s's bundled Traefik lives in kube-system, not ingress-system; allow
  # it to reach the soctalk-system services. Same patch the appliance applies.
  local np
  for np in soctalk-system-ui-ingress-allow soctalk-system-api-ingress-allow; do
    kubectl -n "$NAMESPACE" patch networkpolicy "$np" --type=json \
      -p='[{"op":"add","path":"/spec/ingress/0/from/-","value":{"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":"kube-system"}}}}]' \
      2>/dev/null || true
  done
}

# Onboard a tenant via the API through Traefik. Config from args or, when
# --onboard-env is set, from that sourced file (appliance path).
onboard_tenant() {
  local slug="$1" name="$2" host="$3" email="$4" pw="$5"
  log "Waiting for the API to answer through Traefik (Host: $host)"
  local code api_ok=0
  for _ in $(seq 1 120); do
    code=$(curl -sk -m 5 -o /dev/null -w "%{http_code}" -H "Host: $host" "$API_BASE/api/auth/me" || echo 000)
    case "$code" in 200|401) api_ok=1; break;; esac
    sleep 5
  done
  [[ "$api_ok" == "1" ]] || { warn "API never answered through Traefik; skipping tenant onboard"; return 0; }
  local jar; jar="$(mktemp)"
  if curl -sfk -m 10 -c "$jar" -H "Content-Type: application/json" \
       -H "Host: $host" -H "Origin: https://$host" \
       -d "{\"email\":$(json_str "$email"),\"password\":$(json_str "$pw")}" \
       "$API_BASE/api/auth/login" >/dev/null; then
    log "Onboarding tenant '$slug'"
    curl -sfk -m 30 -b "$jar" -H "Content-Type: application/json" \
      -H "Host: $host" -H "Origin: https://$host" -X POST \
      -d "{\"slug\":$(json_str "$slug"),\"display_name\":$(json_str "$name"),\"profile\":\"poc\"}" \
      "$API_BASE/api/mssp/tenants/onboard" >/dev/null \
      && log "Tenant onboard accepted; provisioning runs async" \
      || warn "tenant onboard POST failed (continuing)"
  else
    warn "bootstrap admin login failed (continuing without tenant)"
  fi
  rm -f "$jar"
}

maybe_onboard() {
  if [[ -n "$ONBOARD_ENV" && -f "$ONBOARD_ENV" ]]; then
    # appliance path: TENANT_SLUG/TENANT_NAME/ADMIN_EMAIL/ADMIN_PW/INGRESS_HOST
    # shellcheck disable=SC1090
    . "$ONBOARD_ENV"
    [[ -n "${TENANT_SLUG:-}" ]] && onboard_tenant "$TENANT_SLUG" "${TENANT_NAME:-$TENANT_SLUG}" \
      "${INGRESS_HOST:-soctalk.local}" "${ADMIN_EMAIL:-}" "${ADMIN_PW:-}"
  elif [[ "$ONBOARD_DEMO" == "true" ]]; then
    local dhost="${HOSTNAME_IN:-soctalk.local}"
    onboard_tenant "demo" "${MSSP_NAME:-Demo} — Demo" "$dhost" "$ADMIN_EMAIL" "$ADMIN_PASSWORD"
  fi
}

print_summary() {
  local pw_line=""
  [[ "$MODE" == "demo" ]] && pw_line=$(printf '  Password:  %s   (demo — change after first login)' "$ADMIN_PASSWORD")
  local host="${HOSTNAME_IN:-soctalk.local}"
  local hint=""
  case "$host" in
    localhost|127.*) : ;;   # reachable as-is (incl. via WSL2 localhost forwarding)
    *) local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
       hint="   ${c_yel}(add '$ip $host' to /etc/hosts, or use your DNS)${c_rst}" ;;
  esac
  cat <<EOF

${c_grn}${c_bold}SocTalk is installed.${c_rst}

  URL:       https://$host/$hint
  Login:     $ADMIN_EMAIL
$pw_line
  Logs:      sudo journalctl -u k3s -f
  Pods:      sudo k3s kubectl -n $NAMESPACE get pods
  Uninstall: sudo /usr/local/bin/k3s-uninstall.sh

EOF
}

# --------------------------------------------------------------------- #
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --demo) MODE="demo"; ASSUME_YES="true";;  # usage promises non-interactive (#108)
      --chart-version) CHART_VERSION="$2"; shift;;
      --chart-dir) CHART_DIR="$2"; MODE="values-file"; shift;;
      --values-file) VALUES_FILE="$2"; MODE="values-file"; shift;;
      --llm-key-file) LLM_KEY_FILE="$2"; shift;;
      --onboard-env) ONBOARD_ENV="$2"; shift;;
      --onboard-demo) ONBOARD_DEMO="true";;
      --skip-preflight) SKIP_PREFLIGHT="true";;
      --skip-consent) SKIP_CONSENT="true";;
      -y|--yes) ASSUME_YES="true";;
      -h|--help) usage; exit 0;;
      *) die "unknown argument: $1 (try --help)";;
    esac
    shift
  done
}

main() {
  parse_args "$@"
  auto_assume_yes_from_env     # before ANY /dev/tty prompt (issue #108)
  need_root                    # k3s (systemd + host install) needs root
  require_systemd              # non-skippable: the whole flow assumes systemd
  [[ "$SKIP_PREFLIGHT" == "true" ]] || preflight
  confirm_changes
  ensure_k3s
  ensure_helm
  if [[ "$MODE" == "values-file" ]]; then
    [[ -n "$VALUES_FILE" ]] || die "--values-file required with --chart-dir"
    create_llm_secret
  else
    prompt_config
    render_values
    create_llm_secret
  fi
  install_chart
  patch_networkpolicy
  maybe_onboard
  print_summary
}

# Run only when executed, not when sourced (firstboot.sh sources this).
if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
