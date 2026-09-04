#!/bin/bash
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
pass=0; fail=0
check() { local name=$1 command=$2 expect=$3 out; out=$(./infra/run_sandboxed.sh "$command" 2>&1 || true); if grep -Eq "$expect" <<<"$out"; then echo "PASS: $name"; ((pass+=1)); else echo "FAIL: $name"; echo "$out"; ((fail+=1)); fi; }
check 'network is unavailable' 'ping -c1 -W1 192.168.8.1 || ping -c1 -W1 1.1.1.1' 'exit=[^0]'
check 'CPU limit terminates workload' 'while :; do :; done' 'exit=(137|152|124)'
check 'read-only root rejects write' 'touch /etc/ecology-escape' 'exit=[^0]'
out=$(./infra/run_sandboxed.sh 'echo teardown-check' 2>&1 || true)
if grep -Eq 'runsc_leftovers=0 .*destroyed=yes' <<<"$out" && qm status 190 | grep -q stopped; then echo 'PASS: sandbox teardown and VM stop'; ((pass+=1)); else echo 'FAIL: sandbox teardown and VM stop'; echo "$out"; ((fail+=1)); fi
echo "Smoke tests: $pass passed, $fail failed"
((fail == 0))
