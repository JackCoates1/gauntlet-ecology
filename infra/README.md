# Gauntlet: Ecology containment spike

This provisions `ecology-runner`, a dedicated QEMU VM (VMID 190) on `homelab-pve`. Untrusted commands execute only inside a fresh gVisor sandbox in that VM.

## What is actually deployed

- A new portless bridge, `vmbr-ecology`, with no IP address, gateway, NAT, or physical uplink. The runner has one virtio NIC on that bridge but does not configure it.
- Proxmox VM firewall is enabled on the VM and NIC with default DROP policy and no allow rules.
- The VM has one vCPU, 3 GiB RAM, a 12 GiB thin disk, no QEMU guest agent, no host directory mounts, no SSH key, and no shared folders. Its only command transport is the QEMU serial socket.
- Each command creates a new gVisor `runsc` sandbox with `--network=none`, a BusyBox-only read-only root filesystem, non-root UID 1000, and a 256 MiB `nodev,nosuid,noexec` `/scratch` tmpfs.
- OCI limits include 512 MiB memory, 100 PIDs, and a 30-second CPU rlimit. A 60-second wall-clock timeout and 1 MiB file-size rlimit apply. Captured stdout/stderr are capped at 1 MiB each.
- The gVisor sandbox and OCI bundle are forcibly deleted after every command, then the VM powers off.

## Important limitations

Firecracker is **not** in use: this node lacks `/dev/kvm` / hardware virtualization. gVisor's ptrace platform is weaker than a hardware-virtualized microVM. The outer QEMU VM is the main host isolation boundary; gVisor adds process, filesystem, resource and network containment within it. This is a homelab containment spike, not cloud-provider-grade protection against a guest kernel, QEMU, hypervisor, or host-kernel zero-day. Do not run secrets or hostile production workloads here.

The VM persists as an inert runner appliance. The *per-command gVisor sandbox* is fresh and is destroyed after every run; this is not a fresh QEMU VM for every command.

## Use

```bash
./infra/provision.sh
./infra/run_sandboxed.sh 'id; echo hello > /scratch/hello; cat /scratch/hello'
./infra/smoke_test.sh
```

`provision.sh` is idempotent. It downloads the Ubuntu cloud image and Debian packages on the **host only**; the guest is never given a route to bootstrap.
