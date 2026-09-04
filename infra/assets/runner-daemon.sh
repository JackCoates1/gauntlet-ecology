#!/bin/bash
set -Eeuo pipefail
exec </dev/ttyS0 >/dev/ttyS0 2>&1
printf 'ECOLOGY_READY\n'
execute_run() {
  local encoded=$1 keep_vm=$2
  run_id="run-$(date +%s)-$RANDOM"; run_dir="/var/lib/ecology-runs/$run_id"
  mkdir -p "$run_dir/rootfs" "$run_dir/scratch"
  if ! printf '%s' "$encoded" | base64 -d >"$run_dir/command.sh" 2>/dev/null; then printf 'ECOLOGY_RESULT id=%s exit=2 reason=invalid-base64 destroyed=yes\n' "$run_id"; rm -rf "$run_dir"; return; fi
  chmod 0555 "$run_dir/command.sh"
  cp -a /opt/ecology/rootfs/. "$run_dir/rootfs/"
  touch "$run_dir/rootfs/command.sh"
  start=$(date +%s); set +e
  (ulimit -t 30 -u 100 -f 2048 -v 524288; timeout -k 3s 60s unshare --mount --pid --ipc --uts --net --fork --mount-proc bash -ceu '
    run_dir=$1
    mount --make-rprivate /
    mount --bind "$run_dir/rootfs" "$run_dir/rootfs"
    mount -o remount,bind,ro "$run_dir/rootfs"
    mount -t tmpfs -o rw,nosuid,nodev,noexec,size=256m tmpfs "$run_dir/rootfs/scratch"
    mount --bind "$run_dir/command.sh" "$run_dir/rootfs/command.sh"
    mount -o remount,bind,ro "$run_dir/rootfs/command.sh"
    mount -t proc -o nosuid,nodev,noexec proc "$run_dir/rootfs/proc"
    mount -t tmpfs -o nosuid,nodev,noexec,size=64m tmpfs "$run_dir/rootfs/dev"
    exec chroot "$run_dir/rootfs" /bin/sh /command.sh
  ' bash "$run_dir") >"$run_dir/stdout" 2>"$run_dir/stderr"; status=$?
  set -e; elapsed=$(( $(date +%s) - start )); stdout_bytes=$(wc -c <"$run_dir/stdout"); stderr_bytes=$(wc -c <"$run_dir/stderr")
  printf 'ECOLOGY_STDOUT_BEGIN id=%s\n' "$run_id"; head -c 1048576 "$run_dir/stdout"; printf '\nECOLOGY_STDOUT_END\n'; printf 'ECOLOGY_STDERR_BEGIN id=%s\n' "$run_id"; head -c 1048576 "$run_dir/stderr"; printf '\nECOLOGY_STDERR_END\n'
  rm -rf "$run_dir"
  printf 'ECOLOGY_RESULT id=%s exit=%s elapsed_s=%s stdout_bytes=%s stderr_bytes=%s sandbox_leftovers=0 destroyed=yes\n' "$run_id" "$status" "$elapsed" "$stdout_bytes" "$stderr_bytes"
  (( keep_vm )) && return
  systemctl poweroff --no-block; exit 0
}

collecting=0
collected_keep_vm=0
collected_payload=""
while IFS= read -r line; do
  if [[ "$line" == ECOLOGY_RUN_KEEP\ * ]]; then
    execute_run "${line#ECOLOGY_RUN_KEEP }" 1
  elif [[ "$line" == ECOLOGY_RUN\ * ]]; then
    execute_run "${line#ECOLOGY_RUN }" 0
  elif [[ "$line" == ECOLOGY_RUN_KEEP_BEGIN ]]; then
    collecting=1; collected_keep_vm=1; collected_payload=""
  elif [[ "$line" == ECOLOGY_RUN_BEGIN ]]; then
    collecting=1; collected_keep_vm=0; collected_payload=""
  elif [[ "$line" == ECOLOGY_RUN_CHUNK\ * && $collecting == 1 ]]; then
    collected_payload+="${line#ECOLOGY_RUN_CHUNK }"
  elif [[ "$line" == ECOLOGY_RUN_END && $collecting == 1 ]]; then
    collecting=0
    execute_run "$collected_payload" "$collected_keep_vm"
    collected_payload=""
  fi
done
