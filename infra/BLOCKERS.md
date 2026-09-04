# Constraints and decisions

- `vmbr1` already exists and is used by `sandbox-agent` (LXC 199). It has outbound NAT and explicit exceptions, which does not meet this project's no-network requirement. It is deliberately untouched. This project uses a new, portless `vmbr-ecology` bridge instead.
- The host has no `/dev/kvm` and exposes no VMX/SVM CPU feature. Firecracker cannot run on this node, so this spike uses the documented weaker fallback: gVisor `runsc` inside a dedicated QEMU VM, with the ptrace platform.
- A 25 GB fully allocated disk would exceed the approximately 20 GB free on the host's `local` filesystem. The runner uses a 12 GB thin-provisioned `local-lvm` disk (the pool had about 298 GB unallocated at provisioning time); its expected consumed size is roughly 3 GB.
