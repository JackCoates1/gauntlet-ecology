#!/bin/bash
set -Eeuo pipefail
[[ $# -eq 1 ]] || { echo "usage: $0 <command>" >&2; exit 2; }
[[ $(id -u) -eq 0 ]] || { echo 'must run as root on homelab-pve' >&2; exit 1; }
VMID=190; sock="/var/run/qemu-server/${VMID}.serial0"; out=$(mktemp); owns_vm=0; preserve_vm=0
cleanup() {
  local status=$?
  if (( owns_vm && ! preserve_vm )) && [[ "$(qm status "$VMID" 2>/dev/null | awk '{print $2}')" == running ]]; then
    qm stop "$VMID" >/dev/null 2>&1 || true
  fi
  rm -f "$out"
  exit "$status"
}
trap cleanup EXIT INT TERM
qm status "$VMID" >/dev/null 2>&1 || { echo 'ecology-runner is not provisioned; run infra/provision.sh' >&2; exit 1; }
keep_vm=${ECOLOGY_KEEP_VM:-0}
[[ $keep_vm == 0 || $keep_vm == 1 ]] || { echo 'ECOLOGY_KEEP_VM must be 0 or 1' >&2; exit 2; }
state=$(qm status "$VMID" | awk '{print $2}')
if [[ $state == stopped ]]; then
  qm start "$VMID"; owns_vm=1; wait_for_ready=1
  for _ in $(seq 1 30); do [[ -S $sock ]] && break; sleep 1; done
  [[ -S $sock ]] || { echo 'serial socket unavailable' >&2; exit 1; }
elif [[ $state == running && $keep_vm == 1 ]]; then
  owns_vm=1; wait_for_ready=0
else
  echo 'ecology-runner is unexpectedly running; refusing concurrent use' >&2; exit 1
fi
payload=$(printf '%s' "$1" | base64 -w0)
export ECOLOGY_PAYLOAD="$payload"
export ECOLOGY_WAIT_FOR_READY="$wait_for_ready"
export ECOLOGY_RUN_PREFIX=$([[ $keep_vm == 1 ]] && printf 'ECOLOGY_RUN_KEEP' || printf 'ECOLOGY_RUN')
set +e
expect -c '
  set wait_for_ready $env(ECOLOGY_WAIT_FOR_READY)
  set payload $env(ECOLOGY_PAYLOAD)
  set run_prefix $env(ECOLOGY_RUN_PREFIX)
  set started [clock seconds]
  spawn socat - UNIX-CONNECT:/var/run/qemu-server/190.serial0
  if {$wait_for_ready == 1} {
    set timeout 300
    expect {
      -re {ECOLOGY_READY} {
        puts "ECOLOGY_HOST ready_elapsed_s=[expr {[clock seconds] - $started}]"
      }
      timeout {
        puts "ECOLOGY_HOST readiness_timeout_s=300"
        exit 1
      }
      eof {
        puts "ECOLOGY_HOST serial_eof_before_ready"
        exit 1
      }
    }
  }
  set timeout 105
  set chunk_size 3000
  if {[string length $payload] <= $chunk_size} {
    send -- "$run_prefix $payload\n"
  } else {
    send -- "${run_prefix}_BEGIN\n"
    for {set index 0} {$index < [string length $payload]} {incr index $chunk_size} {
      send -- "ECOLOGY_RUN_CHUNK [string range $payload $index [expr {$index + $chunk_size - 1}]]\n"
    }
    send -- "ECOLOGY_RUN_END\n"
  }
  expect {
    -re {ECOLOGY_RESULT .*destroyed=yes} { exit 0 }
    timeout {
      puts "ECOLOGY_HOST result_timeout_s=105"
      exit 1
    }
    eof {
      puts "ECOLOGY_HOST serial_eof_before_result"
      exit 1
    }
  }
' >"$out" 2>&1
expect_status=$?
set -e
cat "$out"
(( expect_status == 0 )) || { echo 'runner did not return a result' >&2; exit 1; }
grep -q 'ECOLOGY_RESULT .*destroyed=yes' "$out" || { echo 'runner returned no cleanup proof' >&2; exit 1; }
grep 'ECOLOGY_RESULT' "$out" | tail -1 | grep -Eq 'sandbox_leftovers=0 .*destroyed=yes' || { echo 'sandbox cleanup verification failed' >&2; exit 1; }
if [[ $keep_vm == 1 ]]; then preserve_vm=1; exit 0; fi
qm stop "$VMID" >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
  [[ "$(qm status "$VMID" | awk '{print $2}')" == stopped ]] && { owns_vm=0; exit 0; }
  sleep 1
done
echo 'runner returned a result but VM did not stop' >&2
exit 1
