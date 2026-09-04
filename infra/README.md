# Gauntlet: Ecology containment spike

This provisions `ecology-runner`, a dedicated QEMU VM (VMID 190) on `homelab-pve`. Untrusted commands execute only inside a fresh namespace sandbox in that VM.

## What is actually deployed

- A new portless bridge, `vmbr-ecology`, with no IP address, gateway, NAT, or physical uplink. The runner has no virtual NIC; its only command transport is the QEMU serial socket.
- There is therefore no guest network interface, route, NAT, physical uplink, or host directory mount to expose.
- The VM has one vCPU, 3 GiB RAM, a 12 GiB thin disk, a host CPU model, no QEMU guest agent, no host directory mounts, no SSH key, and no shared folders. Its only command transport is the QEMU serial socket.
- Each command creates a fresh Linux namespace sandbox (mount, PID, IPC, UTS and network namespaces) with a read-only BusyBox/Python runtime root filesystem, a 256 MiB `nodev,nosuid,noexec` `/scratch` tmpfs, 30 CPU seconds, 100 processes, 512 MiB virtual memory, and a 60-second wall-clock timeout.
- OCI limits include 512 MiB memory, 100 PIDs, and a 30-second CPU rlimit. A 60-second wall-clock timeout and 1 MiB file-size rlimit apply. Captured stdout/stderr are capped at 1 MiB each.
- The namespace sandbox's run directory is deleted after every command, then the host wrapper stops the VM.

## Important limitations

Firecracker and gVisor are not in use. The Debian gVisor package's ptrace and systrap platforms both fail in this guest (the latter panics while creating its syscall thread); its KVM platform also exits before sandbox startup. The outer QEMU VM remains the hardware-virtualized host isolation boundary, and the per-command namespace sandbox adds filesystem, process, resource and network containment within it. This is a homelab containment spike, not cloud-provider-grade protection against a guest kernel, QEMU, hypervisor, or host-kernel zero-day. Do not run secrets or hostile production workloads here.

The VM persists as an inert runner appliance. The *per-command namespace sandbox* is fresh and is destroyed after every run; this is not a fresh QEMU VM for every command.

## Use

```bash
./infra/provision.sh
./infra/run_sandboxed.sh 'id; echo hello > /scratch/hello; cat /scratch/hello'
./infra/smoke_test.sh
```

`provision.sh` is idempotent. It downloads the Ubuntu cloud image and Debian packages on the **host only**; the guest is never given a route to bootstrap.

## Boot and serial timing

`/dev/ttyS0` is owned by the `ecology-runner.service` systemd service; it is not
a login console. Provisioning masks `serial-getty@ttyS0.service` in the guest,
installs and enables the service before the first boot, and the service writes
`ECOLOGY_READY` only after its serial command listener has opened the device.
It reads `ECOLOGY_RUN <base64>` (or the smoke-suite-only `ECOLOGY_RUN_KEEP`) and
writes `ECOLOGY_RESULT ...` on that same serial line. No username, password, or
interactive shell is involved.

`run_sandboxed.sh` does not treat the QEMU serial socket as a boot signal. It
holds one serial session open, waits for the guest's `ECOLOGY_READY` marker (up
to 300 seconds), then starts the 105-second command result budget. It also
waits for the requested post-run power-off, so a failed or interrupted call
cannot strand a runner VM and poison the next check.

For the smoke suite only, its first three checks set `ECOLOGY_KEEP_VM=1` and
reuse that ready VM while still creating and deleting a fresh namespace sandbox for
each command. The final check uses the normal one-command lifecycle and proves
that the VM powers off. The first check is always a genuine cold boot because
the suite requires VM 190 to start stopped.

The runner has no network by design. Provisioning uses no virtual NIC and also
masks `systemd-networkd-wait-online.service` inside the guest; the serial daemon
starts after local filesystems rather than after `cloud-final`. This prevents a
networkless appliance from waiting pointlessly for network availability.

To apply changed appliance assets to an existing stopped runner, rebuild only
this dedicated VM:

```bash
./infra/provision.sh --recreate
```
