#!/bin/bash
set -Eeuo pipefail
[[ $# -eq 1 ]] || { echo "usage: $0 <command>" >&2; exit 2; }
[[ $(id -u) -eq 0 ]] || { echo 'must run as root on homelab-pve' >&2; exit 1; }
VMID=190; sock="/var/run/qemu-server/${VMID}.serial0"; out=$(mktemp); trap 'rm -f "$out"' EXIT
qm status "$VMID" >/dev/null 2>&1 || { echo 'ecology-runner is not provisioned; run infra/provision.sh' >&2; exit 1; }
[[ "$(qm status "$VMID" | awk '{print $2}')" == stopped ]] || { echo 'ecology-runner is unexpectedly running; refusing concurrent use' >&2; exit 1; }
qm start "$VMID"
for _ in $(seq 1 90); do [[ -S $sock ]] && break; sleep 1; done
[[ -S $sock ]] || { echo 'serial socket unavailable' >&2; qm stop "$VMID"; exit 1; }
payload=$(printf '%s' "$1" | base64 -w0)
{ printf 'ECOLOGY_RUN %s\n' "$payload"; sleep 75; } | socat - UNIX-CONNECT:"$sock" >"$out" 2>&1 || true
cat "$out"
grep -q 'ECOLOGY_RESULT .*destroyed=yes' "$out" || { echo 'runner returned no cleanup proof' >&2; exit 1; }
grep 'ECOLOGY_RESULT' "$out" | tail -1 | grep -Eq 'runsc_leftovers=0 .*destroyed=yes' || { echo 'sandbox cleanup verification failed' >&2; exit 1; }
