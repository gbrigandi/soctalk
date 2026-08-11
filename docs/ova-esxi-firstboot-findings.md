# OVA on ESXi: first-boot failure and fix

Status: fixed and validated on BOTH boot paths of the shipped v0.2.0 OVA
(final fix at commit c9606f5).

- Seeded / cloud-init path: the packer KVM boot-test passes (firstboot runs to
  completion). This is the path an earlier attempt broke with an ordering cycle
  (see "First fix attempt" below); it is now green.
- No-datasource / ESXi path: the released OVA was deployed to real ESXi 8.0.3
  with NO cloud-init seed and, with no manual intervention, came up fully:
  ens160 pulled DHCP from the shipped name-glob netplan, the setup wizard
  auto-started and served its page on :8443, firstboot started (activating),
  and `journalctl | grep "ordering cycle"` returned 0. This is the exact field
  scenario that failed before the fix.

## What was tested

The v0.2.0 release OVA (`soctalk-demo-0.2.0.ova`) was deployed to a real
VMware ESXi 8.0.3 host (nested on staging) via `govc import.ova`, powered on,
and driven to the setup wizard.

## Result

The artifact itself is sound and the appliance is sound, but the OVA does NOT
come up usable on ESXi out of the box. Two defects block it, both from a single
root cause: the image assumes cloud-init always runs at first boot.

What passed:

- Import into ESXi: clean, about 50 seconds. The OVF and streamOptimized VMDK
  are accepted without complaint.
- VM registration: correct spec (4 vCPU, 8 GB, Ubuntu 64-bit guest, VmxNet3).
- Boot: reaches the Ubuntu login prompt.
- The disk and the appliance work: once networking was corrected live and the
  wizard was started by hand, it served the real "SocTalk first-boot setup"
  page over the network.

What failed on a clean deploy:

- No network. `ens160` stays DOWN, no DHCP, appliance unreachable.
- The setup wizard never starts, so nothing binds :8443 even with network up.

## Root cause

On a real OVA deploy there is no cloud-init datasource (no NoCloud seed ISO,
no cloud metadata service). cloud-init's ds-identify finds nothing and disables
cloud-init (`boot_status_code: disabled-by-generator`). That single fact breaks
two things:

1. Networking. cloud-init wrote `/etc/netplan/50-cloud-init.yaml` during the
   qemu build, pinned to the BUILD NIC:

   ```
   ethernets:
     ens3:
       match: { macaddress: "52:54:00:12:34:56" }
       set-name: "ens3"
       dhcp4: true
   ```

   `cloud-init clean` does not delete this file. On ESXi the NIC is `ens160`
   with a VMware MAC, so the match hits nothing, the interface is never brought
   up, and there is no network. cloud-init being disabled means it never
   regenerates a correct netplan.

2. The setup wizard and firstboot units. Both were `WantedBy=cloud-init.target`.
   When cloud-init is disabled, `cloud-init.target` never activates, so neither
   unit ever starts. The wizard never binds :8443; the helm install never runs.

## Why CI did not catch it

The packer boot-test (`build-packer-images.yml`) attaches a
`cloud-localds seed.iso`, i.e. a NoCloud datasource. That makes cloud-init run,
which regenerates a correct netplan for the boot-test's own NIC and activates
`cloud-init.target` so the units start. A field OVA deploy has no seed, so the
test can never exercise the failing path. The KVM boot-test also reuses the same
qemu MAC, so even the pinned netplan would match there.

## Fix (implemented, needs a rebuild to validate)

- `infra/packer/scripts/install.sh`: after `cloud-init clean`, delete
  `50-cloud-init.yaml` and write `50-soctalk-dhcp.yaml` that DHCPs by name glob
  (`match: name: "e*"`), so any hypervisor NIC name (ens160, ens3, eth0,
  enp1s0) comes up without cloud-init.
- `soctalk-setup-wizard.service` and `soctalk-firstboot.service`: change
  `WantedBy=cloud-init.target` to `WantedBy=multi-user.target`, so they start on
  a normal boot whether or not cloud-init runs, AND remove `After=cloud-init.target`
  entirely. Keeping that `After=` was the first attempt and it was WRONG: with
  `WantedBy=multi-user.target` it forms multi-user -> unit -> cloud-init.target
  -> multi-user, an ordering cycle that systemd breaks by deleting the job, so
  both units get skipped whenever cloud-init IS present (the seeded/cloud path).
  Ordering after cloud-init is unnecessary anyway: firstboot.sh polls for
  values.yaml + llm.key itself (from cloud-init OR the wizard) with the unit at
  TimeoutStartSec=infinity, so start order does not matter.

## First fix attempt and why it was caught

The initial fix kept `After=cloud-init.target` (as "ordering only"). It passed
the manual ESXi test (no cloud-init, so cloud-init.target is not in the
transaction and there is no cycle) but FAILED the packer KVM boot-test, which
seeds cloud-init: the serial log showed
`Found ordering cycle on soctalk-setup-wizard.service` and
`Job ... deleted to break ordering cycle`, so firstboot never ran. The seeded
CI boot-test caught exactly the case the manual ESXi test could not, and the
no-seed ESXi test caught the case CI could not. Both paths are needed; see the
follow-up below.

## Follow-up worth doing

Add a no-seed (or different-MAC) boot to the packer boot-test so this class of
regression is caught in CI, not on a customer's ESXi host.

## Validation still owed

The mechanism of each fix was proven live on the ESXi VM (a correct netplan
brought `ens160` up and pulled DHCP; starting the wizard bound :8443 and served
the page). The exact edited unit files and install step have not yet been
exercised by a fresh packer build plus a clean ESXi redeploy. That rebuild is
the remaining step before the OVA can be called confirmed.
