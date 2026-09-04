#!/bin/bash
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
[[ $(id -u) -eq 0 ]] || { echo 'must run as root on homelab-pve' >&2; exit 1; }
VMID=190; NAME=ecology-runner; BRIDGE=vmbr-ecology; ISO_DIR=/var/lib/vz/template/iso; STAGE=$(mktemp -d)
cleanup() { rm -rf "$STAGE"; }; trap cleanup EXIT
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
  echo "$NAME already provisioned"; exit 0
fi
IMG=$ISO_DIR/noble-server-cloudimg-amd64.img
[[ -s $IMG ]] || curl --fail --location --retry 3 -o "$IMG" https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
apt-get update -qq
(cd "$STAGE" && apt-get download runsc busybox-static)
mkdir -p "$STAGE/rootfs/bin" "$STAGE/rootfs/dev" "$STAGE/rootfs/proc" "$STAGE/rootfs/scratch"
dpkg-deb -x "$STAGE"/busybox-static_*_amd64.deb "$STAGE/busy"
cp "$STAGE/busy/bin/busybox" "$STAGE/rootfs/bin/busybox"
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
runcmd:
  - [ bash, -c, 'mount -o ro /dev/disk/by-label/cidata /mnt || true; dpkg -i /mnt/runsc_*_amd64.deb; mkdir -p /opt/ecology/rootfs /var/lib/ecology-runs; tar -C /opt/ecology/rootfs -xzf /mnt/rootfs.tar.gz; install -m 0755 /mnt/runner-daemon.sh /usr/local/sbin/ecology-runner-daemon; install -m 0644 /mnt/ecology-runner.service /etc/systemd/system/ecology-runner.service; systemctl daemon-reload; systemctl enable ecology-runner.service; touch /var/lib/ecology-provisioned; systemctl poweroff --no-block' ]
EOF
printf 'instance-id: ecology-runner\nlocal-hostname: ecology-runner\n' >"$STAGE/meta-data"
genisoimage -quiet -output "$ISO_DIR/ecology-runner-seed.iso" -volid cidata -joliet -rock "$STAGE"
qm create "$VMID" --name "$NAME" --memory 3072 --cores 1 --cpu cputype=kvm64 --ostype l26 --agent 0 --onboot 0 --net0 "virtio,bridge=$BRIDGE,firewall=1" --serial0 socket --vga serial0 --scsihw virtio-scsi-pci --boot order=scsi0 --protection 1 --tags ecology
qm set "$VMID" --scsi0 local-lvm:0,import-from="$IMG",discard=on,ssd=1
qm resize "$VMID" scsi0 12G
qm set "$VMID" --ide2 "local:iso/ecology-runner-seed.iso,media=cdrom" --ciupgrade 0
mkdir -p "/etc/pve/nodes/$(hostname)/qemu-server"
cat >"/etc/pve/nodes/$(hostname)/qemu-server/$VMID.fw" <<'EOF'
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: DROP
EOF
qm start "$VMID"
echo 'Waiting for networkless first-boot provisioning to complete...'
for _ in $(seq 1 90); do
  if [[ "$(qm status "$VMID" | awk '{print $2}')" == stopped ]]; then echo 'Provisioned ecology-runner.'; exit 0; fi
  sleep 2
done
echo 'Timed out waiting for provisioning; VM left running for diagnosis.' >&2; exit 1
