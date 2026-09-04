#!/bin/bash
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
[[ $(id -u) -eq 0 ]] || { echo 'must run as root on homelab-pve' >&2; exit 1; }
VMID=190; NAME=ecology-runner; BRIDGE=vmbr-ecology; ISO_DIR=/var/lib/vz/template/iso; STAGE=$(mktemp -d); NBD=; GUEST_MOUNT=
RECREATE=0
if [[ ${1:-} == --recreate ]]; then RECREATE=1; shift; fi
[[ $# -eq 0 ]] || { echo "usage: $0 [--recreate]" >&2; exit 2; }
cleanup() {
  [[ -z $GUEST_MOUNT ]] || umount "$GUEST_MOUNT" >/dev/null 2>&1 || true
  [[ -z $NBD ]] || qemu-nbd --disconnect "$NBD" >/dev/null 2>&1 || true
  rm -rf "$STAGE"
}; trap cleanup EXIT
if ! ip link show "$BRIDGE" >/dev/null 2>&1; then
  cat >>/etc/network/interfaces.d/ecology-runner <<EOF
auto $BRIDGE
iface $BRIDGE inet manual
    bridge-ports none
    bridge-stp off
    bridge-fd 0
EOF
  ifup "$BRIDGE"
fi
if qm status "$VMID" >/dev/null 2>&1; then
  [[ "$(qm config "$VMID" | awk -F': ' '/^name:/{print $2}')" == "$NAME" ]] || { echo "VMID $VMID belongs to another VM" >&2; exit 1; }
  if (( ! RECREATE )); then echo "$NAME already provisioned"; exit 0; fi
  [[ "$(qm status "$VMID" | awk '{print $2}')" == stopped ]] || { echo "$NAME must be stopped before --recreate" >&2; exit 1; }
  echo "Recreating $NAME from the current containment assets..."
  qm set "$VMID" --protection 0
  qm destroy "$VMID" --purge 1 --destroy-unreferenced-disks 1
fi
IMG=$ISO_DIR/noble-server-cloudimg-amd64.img
[[ -s $IMG ]] || curl --fail --location --retry 3 -o "$IMG" https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
apt-get update -qq
(cd "$STAGE" && apt-get download busybox-static)
mkdir -p "$STAGE/rootfs/bin" "$STAGE/rootfs/dev" "$STAGE/rootfs/proc" "$STAGE/rootfs/scratch"
dpkg-deb -x "$STAGE"/busybox-static_*_amd64.deb "$STAGE/busy"
cp "$STAGE/busy/usr/bin/busybox" "$STAGE/rootfs/bin/busybox"
for a in sh ash cat echo id sleep touch dd head yes true false ls mkdir rm ping; do ln -s busybox "$STAGE/rootfs/bin/$a"; done
cp infra/assets/runner-daemon.sh "$STAGE/runner-daemon.sh"; cp infra/assets/ecology-runner.service "$STAGE/ecology-runner.service"
chmod 0755 "$STAGE/runner-daemon.sh"
tar -C "$STAGE/rootfs" -czf "$STAGE/rootfs.tar.gz" .
cat >"$STAGE/user-data" <<'EOF'
#cloud-config
package_update: false
package_upgrade: false
write_files:
  - path: /etc/cloud/cloud.cfg.d/99-ecology-network.cfg
    permissions: '0644'
    content: |
      network: {config: disabled}
EOF
printf 'instance-id: ecology-runner\nlocal-hostname: ecology-runner\n' >"$STAGE/meta-data"
genisoimage -quiet -output "$ISO_DIR/ecology-runner-seed.iso" -volid cidata -joliet -rock "$STAGE"
qm create "$VMID" --name "$NAME" --memory 3072 --cores 1 --cpu host --kvm 1 --ostype l26 --agent 0 --onboot 0 --serial0 socket --vga serial0 --scsihw virtio-scsi-pci --boot order=scsi0 --protection 1 --tags ecology
qm set "$VMID" --scsi0 local-lvm:0,import-from="$IMG",discard=on,ssd=1
qm resize "$VMID" scsi0 12G
qm set "$VMID" --ide2 "local:iso/ecology-runner-seed.iso,media=cdrom" --ciupgrade 0
modprobe nbd max_part=8
for candidate in /sys/block/nbd{0..7}; do
  [[ ! -s $candidate/pid ]] || continue
  NBD=/dev/"${candidate##*/}"
  break
done
[[ -n $NBD ]] || { echo 'no unused nbd device available for guest preparation' >&2; exit 1; }
qemu-nbd --format=raw --connect="$NBD" "/dev/pve/vm-${VMID}-disk-0"
partprobe "$NBD"
for _ in $(seq 1 15); do [[ -b "${NBD}p1" ]] && break; sleep 1; done
[[ -b "${NBD}p1" ]] || { echo 'guest root partition did not appear' >&2; exit 1; }
GUEST_MOUNT="$STAGE/guest-root"; mkdir "$GUEST_MOUNT"; mount "${NBD}p1" "$GUEST_MOUNT"
ln -s /dev/null "$GUEST_MOUNT/etc/systemd/system/systemd-networkd-wait-online.service"
mkdir -p "$GUEST_MOUNT/opt/ecology/rootfs" "$GUEST_MOUNT/var/lib/ecology-runs"
tar -C "$GUEST_MOUNT/opt/ecology/rootfs" -xzf "$STAGE/rootfs.tar.gz"
bash infra/assets/install-python-runtime.sh "$GUEST_MOUNT" "$GUEST_MOUNT/opt/ecology/rootfs"
install -D -m 0755 "$STAGE/runner-daemon.sh" "$GUEST_MOUNT/usr/local/sbin/ecology-runner-daemon"
install -D -m 0644 "$STAGE/ecology-runner.service" "$GUEST_MOUNT/etc/systemd/system/ecology-runner.service"
# This serial port is a machine protocol, not a human login.  Mask the cloud
# image's generated serial getty before its first boot so it can never race the
# listener for /dev/ttyS0.
ln -sfn /dev/null "$GUEST_MOUNT/etc/systemd/system/serial-getty@ttyS0.service"
mkdir -p "$GUEST_MOUNT/etc/systemd/system/multi-user.target.wants"
ln -sfn ../ecology-runner.service "$GUEST_MOUNT/etc/systemd/system/multi-user.target.wants/ecology-runner.service"
touch "$GUEST_MOUNT/var/lib/ecology-provisioned"
umount "$GUEST_MOUNT"; GUEST_MOUNT=
qemu-nbd --disconnect "$NBD"; NBD=
mkdir -p "/etc/pve/nodes/$(hostname)/qemu-server"
cat >"/etc/pve/nodes/$(hostname)/qemu-server/$VMID.fw" <<'EOF'
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: DROP
EOF
echo 'Provisioned ecology-runner.'
