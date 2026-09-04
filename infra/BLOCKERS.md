# Constraints and decisions

- `vmbr1` already exists and is used by `sandbox-agent` (LXC 199). It has outbound NAT and explicit exceptions, which does not meet this project's no-network requirement. It is deliberately untouched. This project uses a new, portless `vmbr-ecology` bridge instead.
- The host now has `/dev/kvm` and nested virtualization enabled. Firecracker was not added because its kernel/rootfs lifecycle would add an unvalidated second appliance path. Debian's gVisor package was tested in the dedicated VM but its ptrace, systrap and KVM platforms all exit before sandbox startup; the deployed fallback is a fresh Linux namespace sandbox inside the hardware-virtualized outer VM.
- A 25 GB fully allocated disk would exceed the approximately 20 GB free on the host's `local` filesystem. The runner uses a 12 GB thin-provisioned `local-lvm` disk (the pool had about 298 GB unallocated at provisioning time); its expected consumed size is roughly 3 GB.
