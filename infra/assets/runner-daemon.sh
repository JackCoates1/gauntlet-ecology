#!/bin/bash
set -Eeuo pipefail
exec </dev/ttyS0 >/dev/ttyS0 2>&1
printf 'ECOLOGY_READY\n'
while IFS= read -r line; do
  [[ "$line" == ECOLOGY_RUN\ * ]] || continue
  encoded=${line#ECOLOGY_RUN }; run_id="run-$(date +%s)-$RANDOM"; run_dir="/var/lib/ecology-runs/$run_id"
  mkdir -p "$run_dir/rootfs" "$run_dir/scratch"
  if ! printf '%s' "$encoded" | base64 -d >"$run_dir/command.sh" 2>/dev/null; then printf 'ECOLOGY_RESULT id=%s exit=2 reason=invalid-base64 destroyed=yes\n' "$run_id"; rm -rf "$run_dir"; continue; fi
  chmod 0555 "$run_dir/command.sh"
  cat >"$run_dir/config.json" <<EOF
{"ociVersion":"1.0.2","process":{"terminal":false,"user":{"uid":1000,"gid":1000},"args":["/bin/sh","/command.sh"],"env":["PATH=/bin","HOME=/scratch","TMPDIR=/scratch"],"cwd":"/scratch","rlimits":[{"type":"RLIMIT_CPU","hard":30,"soft":30},{"type":"RLIMIT_NPROC","hard":100,"soft":100},{"type":"RLIMIT_FSIZE","hard":1048576,"soft":1048576}],"noNewPrivileges":true},"root":{"path":"rootfs","readonly":true},"hostname":"ecology","mounts":[{"destination":"/proc","type":"proc","source":"proc","options":["nosuid","noexec","nodev"]},{"destination":"/dev","type":"tmpfs","source":"tmpfs","options":["nosuid","strictatime","mode=755","size=65536k"]},{"destination":"/scratch","type":"tmpfs","source":"tmpfs","options":["rw","nosuid","nodev","noexec","size=256m"]},{"destination":"/command.sh","type":"bind","source":"$run_dir/command.sh","options":["rbind","ro","nosuid","nodev","noexec"]}],"linux":{"resources":{"memory":{"limit":536870912},"pids":{"limit":100}},"namespaces":[{"type":"pid"},{"type":"ipc"},{"type":"uts"},{"type":"mount"},{"type":"network"}]}}
EOF
  cp -a /opt/ecology/rootfs/. "$run_dir/rootfs/"
  start=$(date +%s); set +e
  (ulimit -f 2048; timeout -k 3s 60s runsc --rootless=false --network=none --platform=ptrace run --bundle "$run_dir" "$run_id") >"$run_dir/stdout" 2>"$run_dir/stderr"; status=$?
  set -e; elapsed=$(( $(date +%s) - start )); stdout_bytes=$(wc -c <"$run_dir/stdout"); stderr_bytes=$(wc -c <"$run_dir/stderr")
  printf 'ECOLOGY_STDOUT_BEGIN id=%s\n' "$run_id"; head -c 1048576 "$run_dir/stdout"; printf '\nECOLOGY_STDOUT_END\n'; printf 'ECOLOGY_STDERR_BEGIN id=%s\n' "$run_id"; head -c 1048576 "$run_dir/stderr"; printf '\nECOLOGY_STDERR_END\n'
  runsc --rootless=false delete --force "$run_id" >/dev/null 2>&1 || true; rm -rf "$run_dir"; leftovers=$(runsc --rootless=false list --format '{{.ID}}' 2>/dev/null | wc -l)
  printf 'ECOLOGY_RESULT id=%s exit=%s elapsed_s=%s stdout_bytes=%s stderr_bytes=%s runsc_leftovers=%s destroyed=yes\n' "$run_id" "$status" "$elapsed" "$stdout_bytes" "$stderr_bytes" "$leftovers"
  systemctl poweroff --no-block; exit 0
done
