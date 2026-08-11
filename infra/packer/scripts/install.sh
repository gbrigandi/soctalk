#!/bin/bash
# Provisioning script Packer runs inside the building VM. Lays down
# everything that doesn't depend on customer-supplied config: OS
# updates, k3s, helm, the pre-pulled SocTalk chart, and the first-boot
# systemd unit. Customer-specific values (hostname, LLM key, ingress
# hostnames) land at first boot via cloud-init user-data.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------
# 1. Base packages + housekeeping
# ---------------------------------------------------------------------
apt-get update -y
apt-get upgrade -y
apt-get install -y --no-install-recommends \
  curl ca-certificates jq gnupg

# cloud-init stays enabled. The image must accept user-data on every
# new instance to pick up customer hostname / SSH keys / LLM key.

# ---------------------------------------------------------------------
# 2. k3s — installed but not started. First-boot service starts it
#    after writing customer config.
# ---------------------------------------------------------------------
curl -sfL https://get.k3s.io | INSTALL_K3S_SKIP_START=true sh -

# ---------------------------------------------------------------------
# 3. Helm
# ---------------------------------------------------------------------
curl -sSfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# ---------------------------------------------------------------------
# 4. Pre-pull the SocTalk system chart. Saves the customer's first-boot
#    install from needing internet for the chart artifact itself.
#    Container images still pull at runtime — pre-pulling them would
#    inflate the image to ~5 GB. Documented as a future optimization.
# ---------------------------------------------------------------------
install -d -m 755 /opt/soctalk/charts
# No default: a silent fallback means the image gets LABELLED one version while
# BUNDLING the chart of another, and the appliance then installs a release
# nobody asked for. The release pipeline always passes this explicitly
# (cut-k8s-release -> build-packer-images -> packer); a local build must too.
helm pull oci://ghcr.io/soctalk/charts/soctalk-system \
  --version "${SOCTALK_CHART_VERSION:?SOCTALK_CHART_VERSION must be set (the soctalk-system chart version to bundle) — refusing to guess and ship a mislabelled appliance}" \
  --destination /opt/soctalk/charts \
  --untar
# Result: /opt/soctalk/charts/soctalk-system/  (chart dir, not tarball)

# ---------------------------------------------------------------------
# 5. First-boot machinery — installer script + wizard binary + units
# ---------------------------------------------------------------------
install -m 755 /tmp/firstboot.sh /usr/local/bin/soctalk-firstboot
install -m 644 /tmp/soctalk-firstboot.service /etc/systemd/system/soctalk-firstboot.service

# Shared install core, sourced by soctalk-firstboot (and usable directly).
install -m 755 /tmp/install.sh /usr/local/bin/soctalk-install

install -d -m 755 /etc/soctalk
install -m 644 /tmp/values.example.yaml /etc/soctalk/values.example.yaml

# Setup wizard: opt-in path for customers who don't supply cloud-init
# user-data. Binary is built in a separate CI job and shipped to the
# Packer build via a file provisioner.
if [[ -f /tmp/soctalk-setup-wizard ]]; then
  install -m 755 /tmp/soctalk-setup-wizard /usr/local/bin/soctalk-setup-wizard
  install -m 644 /tmp/soctalk-setup-wizard.service /etc/systemd/system/soctalk-setup-wizard.service
  install -d -m 755 /var/lib/soctalk-wizard
  systemctl enable soctalk-setup-wizard.service
  echo "===> soctalk-setup-wizard installed"
else
  echo "===> NOTE: /tmp/soctalk-setup-wizard not staged; image will require cloud-init user-data"
fi

# Enable but don't start. soctalk-firstboot.service waits for either
# cloud-init user-data OR the setup wizard to drop values.yaml + llm.key.
systemctl enable soctalk-firstboot.service

# ---------------------------------------------------------------------
# 6. Reset cloud-init so the image is reusable. Without this, cloud-init
#    thinks it already ran and skips user-data on the next boot.
# ---------------------------------------------------------------------
cloud-init clean --logs --seed

# ---------------------------------------------------------------------
# 6b. Ship a hypervisor-agnostic netplan and DON'T rely on cloud-init to
#     produce one. cloud-init emitted /etc/netplan/50-cloud-init.yaml during
#     THIS (qemu) build, pinned to the build NIC's MAC + name (ens3 /
#     52:54:00:12:34:56). `cloud-init clean` does NOT delete it. On a real
#     OVA deploy to ESXi/vSphere the NIC is ens160 with a VMware MAC and
#     there is no cloud-init datasource, so cloud-init disables itself and
#     never regenerates the file — the pinned match fails, the interface
#     stays DOWN, and the appliance has no network (setup wizard
#     unreachable). Replace it with a name-glob match that DHCPs whatever
#     ethernet the hypervisor presents (ens160, ens3, eth0, enp1s0, …).
rm -f /etc/netplan/50-cloud-init.yaml
cat > /etc/netplan/50-soctalk-dhcp.yaml <<'NETPLAN'
network:
  version: 2
  ethernets:
    all-ethernets:
      match:
        name: "e*"
      dhcp4: true
      dhcp6: false
      optional: true
NETPLAN
chmod 600 /etc/netplan/50-soctalk-dhcp.yaml

# ---------------------------------------------------------------------
# 7. Final cleanup
# ---------------------------------------------------------------------
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*
rm -rf /tmp/* /var/tmp/* || true

echo "===> Packer build complete. Image is ready."
