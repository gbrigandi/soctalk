packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "~> 1.3"
    }
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = "~> 1.1"
    }
  }
}

variable "version" {
  type        = string
  default     = "dev"
  description = "Image version tag. Used in AMI name and QCOW2 filename."
}

variable "soctalk_chart_version" {
  type = string
  # Deliberately no default. A stale default silently bundles the wrong chart
  # into an image labelled with a different version. The release pipeline
  # always passes this (cut-k8s-release -> build-packer-images); a local build
  # must supply it too: -var soctalk_chart_version=X.Y.Z
  description = "soctalk-system chart version to pre-pull into the image (required; must match the release being built)."
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "ubuntu_release" {
  type    = string
  default = "noble"
}

locals {
  # The same install script provisions both image targets. cloud-init
  # remains enabled on the resulting image so customers' user-data
  # (hostname, SSH keys, LLM key, helm values) is applied at first boot.
  install_scripts = [
    "scripts/install.sh",
  ]
}

# AWS AMI
source "amazon-ebs" "soctalk_demo" {
  ami_name      = "soctalk-demo-${var.version}-${formatdate("YYYYMMDDhhmm", timestamp())}"
  ami_description = "SocTalk demo image. cloud-init customizes hostname / SSH keys / LLM key at first boot."
  instance_type = "t3.xlarge"
  region        = var.aws_region

  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-${var.ubuntu_release}-24.04-amd64-server-*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    most_recent = true
    owners      = ["099720109477"] # Canonical
  }

  ssh_username = "ubuntu"

  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 60
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name        = "soctalk-demo-${var.version}"
    Project     = "soctalk"
    Component   = "demo-image"
    Version     = var.version
  }
}

# QCOW2 for KVM / Proxmox
source "qemu" "soctalk_demo" {
  # Ubuntu cloud image is a qcow2 that already has cloud-init.
  iso_url       = "https://cloud-images.ubuntu.com/${var.ubuntu_release}/current/${var.ubuntu_release}-server-cloudimg-amd64.img"
  iso_checksum  = "file:https://cloud-images.ubuntu.com/${var.ubuntu_release}/current/SHA256SUMS"
  disk_image    = true
  disk_size     = "60G"
  output_directory = "build/qemu"
  vm_name       = "soctalk-demo-${var.version}.qcow2"
  format        = "qcow2"
  accelerator   = "kvm"  # falls back to tcg on hosts without KVM
  cpus          = 4
  memory        = 8192
  headless      = true

  # Boot the image with a cloud-init seed ISO that grants Packer SSH access.
  # cloud-localds (from cloud-image-utils) builds this seed at Packer-build
  # time; see README. Without a seed, the base Ubuntu cloud image won't
  # accept SSH because no user is configured.
  cd_files = ["files/seed/user-data", "files/seed/meta-data"]
  cd_label = "cidata"

  ssh_username = "ubuntu"
  ssh_password = "packer"   # only used during build; removed by cleanup step

  shutdown_command = "echo packer | sudo -S shutdown -P now"

  qemuargs = [
    ["-serial", "mon:stdio"],
  ]
}

build {
  name = "soctalk-demo"
  sources = [
    "source.amazon-ebs.soctalk_demo",
    "source.qemu.soctalk_demo",
  ]

  # Convert the qcow2 to the other common virtualization formats so the
  # same build artifact reaches VMware, Hyper-V, Azure, and generic
  # raw-disk consumers. Runs only on the qemu source (the amazon-ebs
  # source produces an AMI directly and doesn't have a local artifact).
  #
  # Format coverage:
  #   .qcow2 — KVM, QEMU, libvirt, Proxmox (the build output itself)
  #   .vmdk  — VMware ESXi / Workstation / Fusion, VirtualBox
  #   .vhdx  — Microsoft Hyper-V (Windows Server / Windows 10+)
  #   .vhd   — Azure (requires fixed-size, 1 MiB-aligned VHD)
  #   .raw   — generic cloud import (GCP image import, OpenStack, dd)
  # Filter to qemu only: source format is "<source_type>.<source_name>",
  # NOT prefixed with the build name like CLI -only flags are.
  # keep_input_artifact retains the qemu source's qcow2 in build/qemu/
  # after the post-processor runs (default would consume + delete it).
  post-processor "shell-local" {
    only                 = ["qemu.soctalk_demo"]
    keep_input_artifact  = true
    inline_shebang       = "/bin/bash"
    inline = [
      "set -euo pipefail",
      "SRC=build/qemu/soctalk-demo-${var.version}.qcow2",
      "DST=build/dist",
      "mkdir -p \"$DST\"",
      "echo '==> converting to vmdk (streamOptimized for VMware/VBox)'",
      "qemu-img convert -p -O vmdk -o subformat=streamOptimized \"$SRC\" \"$DST/soctalk-demo-${var.version}.vmdk\"",
      "echo '==> converting to vhdx (Hyper-V)'",
      "qemu-img convert -p -O vhdx \"$SRC\" \"$DST/soctalk-demo-${var.version}.vhdx\"",
      "echo '==> converting to vhd (Azure, fixed-size)'",
      "qemu-img convert -p -O vpc -o subformat=fixed,force_size \"$SRC\" \"$DST/soctalk-demo-${var.version}.vhd\"",
      "echo '==> converting to raw'",
      "qemu-img convert -p -O raw \"$SRC\" \"$DST/soctalk-demo-${var.version}.raw\"",
      "echo '==> copying qcow2 to dist'",
      "cp \"$SRC\" \"$DST/soctalk-demo-${var.version}.qcow2\"",
      # Wrap the streamOptimized vmdk in an OVA so ESXi / vSphere can deploy it
      # in one step (Deploy OVF Template / ovftool / govc import.ova) with no
      # manual `govc import.vmdk` conversion. Runs after the vmdk exists.
      "echo '==> building OVA (VMware/vSphere one-step deploy)'",
      "bash scripts/make-ova.sh \"${var.version}\" \"$DST\"",
      "echo '==> dist/'",
      "ls -lh \"$DST\"/",
    ]
  }

  # Stage the first-boot machinery onto the image.
  provisioner "file" {
    source      = "scripts/firstboot.sh"
    destination = "/tmp/firstboot.sh"
  }

  # Shared install core (the repo-root one-command installer). firstboot
  # sources it at install time; the same file is the Linux curl|bash
  # installer. Kept as the single source of truth for the install path.
  provisioner "file" {
    source      = "../../install.sh"
    destination = "/tmp/install.sh"
  }

  provisioner "file" {
    source      = "files/soctalk-firstboot.service"
    destination = "/tmp/soctalk-firstboot.service"
  }

  provisioner "file" {
    source      = "files/values.example.yaml"
    destination = "/tmp/values.example.yaml"
  }

  # Setup wizard service unit (always staged).
  provisioner "file" {
    source      = "files/soctalk-setup-wizard.service"
    destination = "/tmp/soctalk-setup-wizard.service"
  }

  # Setup wizard binary. Built by .github/workflows/build-packer-images.yml
  # in a preceding job and dropped into infra/packer/build-artifacts/.
  # We use `direction = "upload"` and skip if the file doesn't exist
  # (local Packer runs without the artifact still produce a working image
  # — just without the wizard, fall back to cloud-init only).
  provisioner "file" {
    source      = "build-artifacts/soctalk-setup-wizard"
    destination = "/tmp/soctalk-setup-wizard"
    # If the binary is missing, this fails. CI always provides it;
    # local builds without it should `touch infra/packer/build-artifacts/soctalk-setup-wizard`
    # or build the wizard first.
  }

  # Install k3s + helm + first-boot service. Pre-pull the SocTalk chart
  # so first boot doesn't need internet for the helm artifact.
  provisioner "shell" {
    environment_vars = [
      "SOCTALK_CHART_VERSION=${var.soctalk_chart_version}",
    ]
    scripts          = local.install_scripts
    execute_command  = "echo packer | sudo -S env {{ .Vars }} bash '{{ .Path }}'"
    expect_disconnect = false
  }
}
