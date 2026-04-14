#!/usr/bin/env bash
set -euo pipefail

namespace="${PE_NAMESPACE:-puppet}"
release="${PE_RELEASE:-pe}"
frontdoor_service="${PE_FRONTDOOR_SERVICE:-pe}"
frontdoor_host="${PE_FRONTDOOR_HOST:-puppet.eyrie}"
compiler_server="${PE_COMPILER_SERVER:-pe-compiler.eyrie}"
test_node_pod="${PE_TEST_NODE_POD:-test-node-puppet-agent-0}"
wait_seconds="${PE_FAILOVER_WAIT_SECONDS:-240}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

active_frontdoor_pod() {
  kubectl -n "$namespace" get endpointslice \
    -l "kubernetes.io/service-name=${frontdoor_service}" \
    -o jsonpath='{range .items[*].endpoints[*]}{.targetRef.name}{"\n"}{end}' \
    | sed '/^$/d' | head -n1
}

wait_for_frontdoor() {
  local previous="${1:-}"
  local deadline=$((SECONDS + wait_seconds))
  local current=""
  while (( SECONDS < deadline )); do
    current="$(active_frontdoor_pod || true)"
    if [[ -n "$current" ]]; then
      if [[ -z "$previous" || "$current" != "$previous" ]]; then
        printf '%s\n' "$current"
        return 0
      fi
    fi
    sleep 2
  done
  echo "timed out waiting for service/${frontdoor_service} failover" >&2
  return 1
}

agent_certname() {
  kubectl -n "$namespace" exec "$test_node_pod" -- puppet config print certname
}

run_console_check() {
  local status
  status="$(curl -sk -o /dev/null -w '%{http_code}' "https://${frontdoor_host}/auth/login")"
  [[ "$status" == "200" ]]
}

wait_for_console_check() {
  local deadline=$((SECONDS + wait_seconds))
  local attempt=1

  while (( SECONDS < deadline )); do
    if run_console_check; then
      return 0
    fi
    echo "[info] console login page attempt ${attempt} is waiting for front door stability"
    attempt=$((attempt + 1))
    sleep 2
  done

  echo "timed out waiting for https://${frontdoor_host}/auth/login" >&2
  return 1
}

run_agent_check() {
  kubectl -n "$namespace" exec "$test_node_pod" -- \
    puppet agent -t --server "$compiler_server"
}

run_with_admin_token() {
  local pod="$1"
  local remote_cmd
  shift
  remote_cmd="$(printf '%q ' "$@")"
  kubectl -n "$namespace" exec "$pod" -c puppetserver -- /bin/sh -lc "
set -e
password=\$(python3 - <<'PY'
import re

for path in [
    '/var/lib/pe-k8s/install/pe.conf',
    '/etc/puppetlabs/enterprise.conf',
]:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            content = handle.read()
    except OSError:
        continue
    match = re.search(r'\"console_admin_password\"\\s*=\\s*\"([^\"]+)\"', content)
    if match:
        print(match.group(1))
        raise SystemExit(0)

raise SystemExit(1)
PY
)
token=\$(/usr/bin/curl -sk \
  -H 'Content-Type: application/json' \
  --request POST \
  https://127.0.0.1:4433/rbac-api/v1/auth/token \
  --data \"{\\\"login\\\":\\\"admin\\\",\\\"password\\\":\\\"\${password}\\\",\\\"lifetime\\\":\\\"5m\\\",\\\"label\\\":\\\"pe-k8s-failover\\\"}\" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"token\"])')
mkdir -p /root/.puppetlabs
printf %s \"\$token\" > /root/.puppetlabs/token
exec ${remote_cmd}
"
}

run_task_check() {
  local pod="$1"
  local certname="$2"
  run_with_admin_token "$pod" \
    /opt/puppetlabs/bin/puppet task run --nodes "$certname" facts
}

run_plan_check() {
  local pod="$1"
  local certname="$2"
  run_with_admin_token "$pod" \
    /opt/puppetlabs/bin/puppet plan run facts::info targets="$certname"
}

run_code_deploy_check() {
  local pod="$1"
  local output_file="$tmp_dir/code-deploy-${pod}.log"
  local status_file="$tmp_dir/code-deploy-status-${pod}.json"
  local rc=0
  run_with_admin_token "$pod" \
    /usr/bin/timeout 60s /opt/puppetlabs/bin/puppet code deploy production --wait >"$output_file" \
    || rc=$?
  if [[ "$rc" -ne 0 && "$rc" -ne 124 ]]; then
    cat "$output_file" >&2
    return "$rc"
  fi
  if [[ "$rc" -eq 124 ]]; then
    echo "[info] puppet code deploy timed out; checking Code Manager status directly"
  fi
  run_with_admin_token "$pod" \
    /bin/sh -lc 'token=$(cat /root/.puppetlabs/token); exec /usr/bin/curl -sk -H "Accept: application/json" -H "X-Authentication: ${token}" https://127.0.0.1:8170/code-manager/v1/deploys/status' >"$status_file" \
    || { cat "$status_file" >&2; return 1; }
  python3 - "$status_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

entries = []
if isinstance(payload, list):
    entries.extend(payload)
elif isinstance(payload, dict):
    entries.extend(payload.get("file-sync-storage-status", {}).get("deployed", []))
    entries.extend(payload.get("deploys-status", {}).get("deploying", []))
    entries.extend(payload.get("deploys-status", {}).get("queued", []))
    entries.extend(payload.get("deploys-status", {}).get("new", []))
    entries.extend(payload.get("deploys-status", {}).get("failed", []))
    entries.append(payload)

for entry in entries:
    if not isinstance(entry, dict):
        continue
    if (entry.get("environment") or "").strip() != "production":
        continue
    deploy_signature = (entry.get("deploy-signature") or "").strip()
    status = (entry.get("status") or "").strip()
    if not status and deploy_signature:
        status = "complete"
    if status != "complete" or not deploy_signature:
        raise SystemExit(f"production deploy status is not complete: {json.dumps(entry, sort_keys=True)}")
    print(f"[ok] code deploy completed: {deploy_signature}")
    raise SystemExit(0)

raise SystemExit("production deploy status not found")
PY
}

summarize_job_output() {
  local label="$1"
  local output_file="$2"

  echo "[ok] ${label}"
  sed -n '/New job ID:/p' "$output_file" | tail -n1
  sed -n '/New Plan Job ID:/p' "$output_file" | tail -n1
  sed -n '/Job completed\./p' "$output_file" | tail -n1
  sed -n '/Duration:/p' "$output_file" | tail -n1
}

run_orchestration_check() {
  local pod="$1"
  local certname="$2"
  local phase="$3"
  local task_output="$tmp_dir/task-${phase}-${pod}.log"
  local plan_output="$tmp_dir/plan-${phase}-${pod}.log"
  local deadline=$((SECONDS + wait_seconds))
  local attempt=1

  echo "[check] task run via ${pod}"
  until run_task_check "$pod" "$certname" >"$task_output"; do
    if [[ "$phase" != "post-failover" ]]; then
      cat "$task_output" >&2
      return 1
    fi
    if ! grep -q "either disconnected or does not have a connection type" "$task_output"; then
      cat "$task_output" >&2
      return 1
    fi
    if (( SECONDS >= deadline )); then
      cat "$task_output" >&2
      return 1
    fi
    echo "[info] task run attempt ${attempt} is waiting for PCP/orchestration reconnection"
    attempt=$((attempt + 1))
    sleep 5
  done
  summarize_job_output "task run completed" "$task_output"

  echo "[check] plan run via ${pod}"
  attempt=1
  until run_plan_check "$pod" "$certname" >"$plan_output"; do
    if [[ "$phase" != "post-failover" ]]; then
      cat "$plan_output" >&2
      return 1
    fi
    if ! grep -q "either disconnected or does not have a connection type" "$plan_output"; then
      cat "$plan_output" >&2
      return 1
    fi
    if (( SECONDS >= deadline )); then
      cat "$plan_output" >&2
      return 1
    fi
    echo "[info] plan run attempt ${attempt} is waiting for PCP/orchestration reconnection"
    attempt=$((attempt + 1))
    sleep 5
  done
  summarize_job_output "plan run completed" "$plan_output"
}

run_checks() {
  local pod="$1"
  local certname="$2"
  local phase="$3"

  echo "[check] frontdoor status"
  "$repo_root/scripts/pe-frontdoor-status.sh" "$namespace" "$release" "$frontdoor_service"

  echo "[check] console login page"
  wait_for_console_check

  echo "[check] code deploy via ${pod}"
  run_code_deploy_check "$pod"

  run_orchestration_check "$pod" "$certname" "$phase"

  echo "[check] agent run via ${compiler_server}"
  run_agent_check
}

certname="$(agent_certname)"
initial_active="$(wait_for_frontdoor)"

echo "[info] initial active backend: ${initial_active}"
run_checks "$initial_active" "$certname" initial

echo "[action] deleting active backend ${initial_active}"
kubectl -n "$namespace" delete pod "$initial_active" --wait=false

new_active="$(wait_for_frontdoor "$initial_active")"
echo "[info] new active backend: ${new_active}"
run_checks "$new_active" "$certname" post-failover

echo "[ok] failover validation completed"
