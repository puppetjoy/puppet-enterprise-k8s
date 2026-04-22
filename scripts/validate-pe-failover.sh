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
ca_test_pod=""
ca_test_certname=""
ca_test_serial=""

cleanup() {
  if [[ -n "${ca_test_pod}" ]]; then
    kubectl -n "$namespace" delete pod "$ca_test_pod" --ignore-not-found >/dev/null 2>&1 || true
  fi
  rm -rf "$tmp_dir"
}

trap cleanup EXIT

active_frontdoor_pod() {
  kubectl -n "$namespace" get endpointslice \
    -l "kubernetes.io/service-name=${frontdoor_service}" \
    -o jsonpath='{range .items[*].endpoints[*]}{.targetRef.name}{"\n"}{end}' \
    | sed '/^$/d' | head -n1
}

pod_frontdoor_eligible() {
  local pod="$1"
  kubectl -n "$namespace" get pod "$pod" \
    -o jsonpath='{.metadata.annotations.pe-k8s\.puppet\.com/frontdoor-eligible}' 2>/dev/null
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

wait_for_standby_reentry() {
  local pod="$1"
  local deadline=$((SECONDS + wait_seconds))
  local current_active=""
  local eligible=""

  while (( SECONDS < deadline )); do
    if kubectl -n "$namespace" get pod "$pod" >/dev/null 2>&1; then
      eligible="$(pod_frontdoor_eligible "$pod" || true)"
      current_active="$(active_frontdoor_pod || true)"
      if [[ "${eligible}" == "true" && "${current_active}" != "${pod}" ]]; then
        wait_for_pod_ready "$pod"
        return 0
      fi
    fi
    sleep 2
  done

  echo "timed out waiting for standby re-entry on ${pod}" >&2
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
  local output_file="${tmp_dir}/agent-run.log"
  local rc=0

  kubectl -n "$namespace" exec "$test_node_pod" -- \
    puppet agent -t --server "$compiler_server" >"${output_file}" 2>&1 || rc=$?

  if [[ "${rc}" -ne 0 && "${rc}" -ne 2 ]]; then
    cat "${output_file}" >&2 || true
    return "${rc}"
  fi

  cat "${output_file}"
}

issue_admin_token() {
  local pod="$1"
  local deadline=$((SECONDS + 60))
  local token=""
  local remote_script

  read -r -d '' remote_script <<'SH' || true
set -e
password=$(python3 - <<'PY'
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
    match = re.search(r'"console_admin_password"\s*=\s*"([^"]+)"', content)
    if match:
        print(match.group(1))
        raise SystemExit(0)

raise SystemExit(1)
PY
)
cat > /tmp/pe-k8s-token.json <<EOF
{"login":"admin","password":"${password}","lifetime":"5m","label":"pe-k8s-failover-$(date +%s%N)"}
EOF
response="$(
/usr/bin/curl -sk \
  -H 'Content-Type: application/json' \
  --request POST \
  https://127.0.0.1:4433/rbac-api/v1/auth/token \
  --data @/tmp/pe-k8s-token.json
)"
python3 -c 'import json, sys
try:
    payload = json.loads(sys.argv[1])
except json.JSONDecodeError:
    raise SystemExit(1)

token = (payload.get("token") or "").strip()
if not token:
    raise SystemExit(1)

print(token)' "$response"
SH

  while (( SECONDS < deadline )); do
    token="$(
      kubectl -n "$namespace" exec "$pod" -c puppetserver -- /bin/sh -lc "$remote_script" 2>/dev/null
    )" || true

    if [[ -n "${token}" ]]; then
      printf '%s\n' "${token}"
      return 0
    fi

    sleep 2
  done

  echo "timed out issuing admin token on ${pod}" >&2
  return 1
}

run_with_admin_token() {
  local pod="$1"
  local remote_cmd
  local token
  shift
  remote_cmd="$(printf '%q ' "$@")"
  token="$(issue_admin_token "$pod")"
  kubectl -n "$namespace" exec "$pod" -c puppetserver -- /bin/sh -lc "
set -e
mkdir -p /root/.puppetlabs
printf %s \"$token\" > /root/.puppetlabs/token
exec ${remote_cmd}
"
}

wait_for_pod_ready() {
  local pod="$1"
  kubectl -n "$namespace" wait --for=condition=Ready "pod/${pod}" --timeout="${wait_seconds}s" >/dev/null
}

pe_service_pods() {
  kubectl -n "$namespace" get pods \
    -l "app.kubernetes.io/instance=${release},app.kubernetes.io/component=pe-services" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
    | sed '/^$/d'
}

compiler_pods() {
  kubectl -n "$namespace" get pods \
    -l "app.kubernetes.io/instance=${release},app.kubernetes.io/component=compiler" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
    | sed '/^$/d'
}

test_node_image() {
  kubectl -n "$namespace" get pod "$test_node_pod" -o jsonpath='{.spec.containers[0].image}'
}

test_node_node_name() {
  kubectl -n "$namespace" get pod "$test_node_pod" -o jsonpath='{.spec.nodeName}'
}

create_ca_test_agent_pod() {
  local image node_name
  image="$(test_node_image)"
  node_name="$(test_node_node_name)"
  ca_test_pod="${release}-ha-ca-$(date +%s)"
  ca_test_certname="${ca_test_pod}.test.puppet"

  cat > "${tmp_dir}/ca-test-agent.yaml" <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${ca_test_pod}
  labels:
    app.kubernetes.io/instance: ${release}
    app.kubernetes.io/component: failover-ca-check
spec:
  restartPolicy: Never
  nodeName: ${node_name}
  containers:
    - name: agent
      image: ${image}
      imagePullPolicy: IfNotPresent
      command:
        - /bin/sh
        - -lc
        - exec sleep infinity
      env:
        - name: PE_AGENT_NODE_NAME
          value: ${ca_test_pod}
        - name: PE_AGENT_CERTNAME
          value: ${ca_test_certname}
        - name: PE_AGENT_SERVER
          value: ${compiler_server}
        - name: PE_AGENT_CA_SERVER
          value: ${frontdoor_service}
        - name: PE_AGENT_PACKAGE_REPO_URL
          value: https://${frontdoor_service}:8140/packages/current/el-9-x86_64.repo
        - name: PE_AGENT_PACKAGE_REPO_SSLVERIFY
          value: "false"
        - name: PE_AGENT_WAITFORCERT
          value: "5"
        - name: PE_AGENT_SPLAY
          value: "false"
        - name: PE_AGENT_PXP_ENABLED
          value: "false"
      volumeMounts:
        - name: etc
          mountPath: /etc/puppetlabs
        - name: cache
          mountPath: /opt/puppetlabs/puppet/cache
        - name: logs
          mountPath: /var/log/puppetlabs
  volumes:
    - name: etc
      emptyDir: {}
    - name: cache
      emptyDir: {}
    - name: logs
      emptyDir: {}
EOF

  kubectl -n "$namespace" apply -f "${tmp_dir}/ca-test-agent.yaml" >/dev/null
}

dump_ca_test_agent_logs() {
  [[ -n "${ca_test_pod}" ]] || return 0
  kubectl -n "$namespace" logs "$ca_test_pod" --tail=200 >&2 || true
}

cert_status_json() {
  local pod="$1"
  local certname="$2"
  local token

  token="$(issue_admin_token "$pod")"
  kubectl -n "$namespace" exec "$pod" -c puppetserver -- \
    curl -sk \
      -H "Accept: application/json" \
      -H "X-Authentication: ${token}" \
      "https://127.0.0.1:8140/puppet-ca/v1/certificate_status/${certname}"
}

cert_state() {
  local pod="$1"
  local certname="$2"
  local response_path="${tmp_dir}/cert-status-${pod}-${certname//[^A-Za-z0-9_.-]/_}.json"
  local state

  if ! cert_status_json "$pod" "$certname" > "${response_path}" 2>/dev/null; then
    return 1
  fi

  if ! state="$(python3 - "${response_path}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)

state = (payload.get("state") or "").strip()
if not state:
    raise SystemExit(1)

print(state)
PY
  )"; then
    return 1
  fi

  [ -n "${state}" ] || return 1
  printf '%s\n' "${state}"
}

wait_for_cert_state() {
  local pod="$1"
  local certname="$2"
  local expected_state="$3"
  local deadline=$((SECONDS + wait_seconds))
  local current_state=""

  while (( SECONDS < deadline )); do
    current_state="$(cert_state "$pod" "$certname" || true)"
    if [[ "${current_state}" == "${expected_state}" ]]; then
      return 0
    fi
    sleep 2
  done

  echo "timed out waiting for certificate ${certname} to reach state ${expected_state}; last state=${current_state:-missing}" >&2
  return 1
}

set_cert_state() {
  local pod="$1"
  local certname="$2"
  local desired_state="$3"
  local token http_code

  token="$(issue_admin_token "$pod")"
  http_code="$(
    kubectl -n "$namespace" exec "$pod" -c puppetserver -- \
      curl -sk \
        -o /dev/null \
        -w '%{http_code}' \
        -X PUT \
        -H "Content-Type: application/json" \
        -H "X-Authentication: ${token}" \
        --data "{\"desired_state\":\"${desired_state}\"}" \
        "https://127.0.0.1:8140/puppet-ca/v1/certificate_status/${certname}"
  )"

  [[ "${http_code}" == "200" || "${http_code}" == "204" ]]
}

clean_cert() {
  local pod="$1"
  local certname="$2"
  local token http_code

  token="$(issue_admin_token "$pod")"
  http_code="$(
    kubectl -n "$namespace" exec "$pod" -c puppetserver -- \
      curl -sk \
        -o /dev/null \
        -w '%{http_code}' \
        -X DELETE \
        -H "X-Authentication: ${token}" \
        "https://127.0.0.1:8140/puppet-ca/v1/certificate_status/${certname}"
  )"

  [[ "${http_code}" == "200" || "${http_code}" == "204" || "${http_code}" == "404" ]]
}

wait_for_temp_agent_certificate() {
  local deadline=$((SECONDS + wait_seconds))

  while (( SECONDS < deadline )); do
    if kubectl -n "$namespace" exec "$ca_test_pod" -- \
      /opt/puppetlabs/bin/puppet ssl verify >/dev/null 2>&1
    then
      return 0
    fi
    sleep 2
  done

  dump_ca_test_agent_logs
  echo "timed out waiting for temporary agent certificate ${ca_test_certname}" >&2
  return 1
}

prepare_ca_test_agent() {
  wait_for_pod_ready "$ca_test_pod"
  kubectl -n "$namespace" exec "$ca_test_pod" -- \
    /usr/local/bin/pe-agent-entrypoint install >/dev/null
}

start_ca_test_agent_bootstrap() {
  kubectl -n "$namespace" exec "$ca_test_pod" -- /bin/sh -lc '
set -e
/opt/puppetlabs/bin/puppet ssl bootstrap \
  --certname "'"${ca_test_certname}"'" \
  --ca_server "'"${frontdoor_service}"'" \
  >/tmp/pe-failover-bootstrap.log 2>&1 &
echo $! > /tmp/pe-failover-bootstrap.pid
'
}

run_ca_test_agent_once() {
  kubectl -n "$namespace" exec "$ca_test_pod" -- \
    /opt/puppetlabs/bin/puppet agent -t --server "$compiler_server"
}

agent_certificate_serial() {
  kubectl -n "$namespace" exec "$ca_test_pod" -- /bin/sh -lc \
    "openssl x509 -in /etc/puppetlabs/puppet/ssl/certs/${ca_test_certname}.pem -noout -serial | cut -d= -f2"
}

wait_for_crl_serial() {
  local pod="$1"
  local container="$2"
  local serial="$3"
  local path_cmd="$4"
  local deadline=$((SECONDS + wait_seconds))

  while (( SECONDS < deadline )); do
    if kubectl -n "$namespace" exec "$pod" -c "$container" -- /bin/sh -lc \
      "crl_path=\$(${path_cmd}) && openssl crl -inform PEM -text -noout -in \"\${crl_path}\" | grep -Fi \"Serial Number: ${serial}\" >/dev/null" \
      >/dev/null 2>&1
    then
      return 0
    fi
    sleep 5
  done

  echo "timed out waiting for CRL serial ${serial} on ${pod}/${container}" >&2
  return 1
}

wait_for_control_plane_crl_serial() {
  local pod="$1"
  local serial="$2"
  wait_for_crl_serial \
    "$pod" \
    puppetserver \
    "$serial" \
    "/opt/puppetlabs/bin/puppet config print hostcrl --section server"
}

wait_for_compiler_crl_serial() {
  local pod="$1"
  local serial="$2"
  wait_for_crl_serial \
    "$pod" \
    puppetserver \
    "$serial" \
    "printf '%s\\n' /etc/puppetlabs/puppet/ssl/crl.pem"
}

wait_for_revoked_agent_failure() {
  local deadline=$((SECONDS + wait_seconds))
  local output_file="${tmp_dir}/revoked-agent-check.log"
  local rc=0

  while (( SECONDS < deadline )); do
    rc=0
    run_ca_test_agent_once >"${output_file}" 2>&1 || rc=$?

    if [[ "${rc}" -ne 0 ]] && grep -Eqi 'revoked|certificate verify failed|SSL_connect returned=1|certificate unknown|Could not select a functional puppet server' "${output_file}"; then
      echo "[ok] revoked agent certificate was rejected"
      sed -n '1,20p' "${output_file}"
      return 0
    fi

    sleep 5
  done

  cat "${output_file}" >&2 || true
  echo "timed out waiting for revoked agent certificate rejection" >&2
  return 1
}

run_ca_enrollment_check() {
  local pod="$1"
  local agent_run_output="${tmp_dir}/ca-agent-run.log"
  local rc=0

  echo "[check] fresh agent enrollment via ${pod}"
  create_ca_test_agent_pod
  prepare_ca_test_agent
  start_ca_test_agent_bootstrap
  wait_for_cert_state "$pod" "$ca_test_certname" requested

  if ! set_cert_state "$pod" "$ca_test_certname" signed; then
    dump_ca_test_agent_logs
    echo "failed to sign ${ca_test_certname} via ${pod}" >&2
    return 1
  fi

  wait_for_cert_state "$pod" "$ca_test_certname" signed
  wait_for_temp_agent_certificate

  echo "[check] freshly enrolled agent run via ${compiler_server}"
  rc=0
  run_ca_test_agent_once >"${agent_run_output}" 2>&1 || rc=$?
  if [[ "${rc}" -ne 0 && "${rc}" -ne 2 ]]; then
    cat "${agent_run_output}" >&2 || true
    return "${rc}"
  fi
  sed -n '1,20p' "${agent_run_output}"

  ca_test_serial="$(agent_certificate_serial)"
  echo "[ok] fresh agent certificate ${ca_test_certname} serial ${ca_test_serial}"
}

run_ca_revocation_check() {
  local pod="$1"
  local original_pod="$2"
  local compiler_pod

  echo "[check] revoke and clean ${ca_test_certname} via ${pod}"
  set_cert_state "$pod" "$ca_test_certname" revoked
  clean_cert "$pod" "$ca_test_certname"

  wait_for_pod_ready "$pod"
  wait_for_pod_ready "$original_pod"

  for control_plane_pod in $(pe_service_pods); do
    wait_for_control_plane_crl_serial "$control_plane_pod" "$ca_test_serial"
  done

  for compiler_pod in $(compiler_pods); do
    wait_for_compiler_crl_serial "$compiler_pod" "$ca_test_serial"
  done

  echo "[check] revoked certificate rejection via ${compiler_server}"
  wait_for_revoked_agent_failure
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

run_puppet_job_check() {
  local pod="$1"
  local certname="$2"
  local description="$3"
  run_with_admin_token "$pod" \
    /opt/puppetlabs/bin/puppet job run --nodes "$certname" --noop --description "$description" --format json
}

show_puppet_job_check() {
  local pod="$1"
  local job_id="$2"
  run_with_admin_token "$pod" \
    /opt/puppetlabs/bin/puppet job show "$job_id" --format json
}

extract_job_id() {
  local output_file="$1"
  python3 - "$output_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

job_id = str(payload.get("job_id") or "").strip()
if not job_id:
    raise SystemExit(1)

print(job_id)
PY
}

validate_job_show_output() {
  local output_file="$1"
  local job_id="$2"
  local certname="$3"
  local description="$4"

  python3 - "$output_file" "$job_id" "$certname" "$description" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

job_id = sys.argv[2]
certname = sys.argv[3]
description = sys.argv[4]

if str(payload.get("job_id") or "").strip() != job_id:
    raise SystemExit("job_id mismatch")

options = payload.get("options") or {}
if (options.get("description") or "") != description:
    raise SystemExit("job description mismatch")

scope = (options.get("scope") or {}).get("nodes") or []
if certname not in scope:
    raise SystemExit("job scope mismatch")

items = payload.get("items") or []
if not any((item.get("name") or "") == certname for item in items if isinstance(item, dict)):
    raise SystemExit("job items missing target node")
PY
}

run_job_history_sync_check() {
  local active_pod="$1"
  local viewer_pod="$2"
  local certname="$3"
  local phase="$4"
  local create_output="$tmp_dir/job-create-${phase}-${active_pod}.json"
  local show_output="$tmp_dir/job-show-${phase}-${viewer_pod}.json"
  local description="pe-k8s-${phase}-job-history"
  local deadline=$((SECONDS + wait_seconds))
  local attempt=1
  local job_id=""

  echo "[check] job history sync ${active_pod} -> ${viewer_pod}"
  run_puppet_job_check "$active_pod" "$certname" "$description" >"$create_output"
  job_id="$(extract_job_id "$create_output")" || {
    cat "$create_output" >&2
    return 1
  }

  while (( SECONDS < deadline )); do
    if show_puppet_job_check "$viewer_pod" "$job_id" >"$show_output" 2>&1; then
      if validate_job_show_output "$show_output" "$job_id" "$certname" "$description"; then
        echo "[ok] job ${job_id} visible on ${viewer_pod}"
        return 0
      fi
    fi

    if grep -q "Authentication token has been revoked" "$show_output" 2>/dev/null; then
      echo "[info] job history attempt ${attempt} is waiting for auth stability"
    elif grep -q "No job found" "$show_output" 2>/dev/null; then
      echo "[info] job history attempt ${attempt} is waiting for shared-state convergence"
    else
      echo "[info] job history attempt ${attempt} is waiting for standby visibility"
    fi
    attempt=$((attempt + 1))
    sleep 5
  done

  cat "$show_output" >&2 || true
  return 1
}

run_rbac_projection_check() {
  local active_pod="$1"
  local standby_pod="$2"
  local phase="$3"

  echo "[check] RBAC user sync ${active_pod} -> ${standby_pod} (${phase})"
  python3 "$repo_root/scripts/validate-rbac-projection.py" \
    --namespace "$namespace" \
    --writer-pod "$active_pod" \
    --reader-pod "$standby_pod" \
    --wait-seconds "$wait_seconds"
}

run_code_deploy_check() {
  local pod="$1"
  local output_file="$tmp_dir/code-deploy-${pod}.log"
  local status_file="$tmp_dir/code-deploy-status-${pod}.json"
  local deadline=$((SECONDS + wait_seconds))
  local attempt=1
  local rc=0

  while :; do
    rc=0
    run_with_admin_token "$pod" \
      /usr/bin/timeout 60s /opt/puppetlabs/bin/puppet code deploy production --wait >"$output_file" \
      || rc=$?

    if [[ "$rc" -ne 0 && "$rc" -ne 124 ]]; then
      if grep -q "Authentication token has been revoked" "$output_file" 2>/dev/null && (( SECONDS < deadline )); then
        echo "[info] code deploy attempt ${attempt} is waiting for post-failover auth stability"
        attempt=$((attempt + 1))
        sleep 5
        continue
      fi
      cat "$output_file" >&2
      return "$rc"
    fi

    if [[ "$rc" -eq 124 ]]; then
      echo "[info] puppet code deploy timed out; checking Code Manager status directly"
    fi

    if run_with_admin_token "$pod" \
      /bin/sh -lc 'token=$(cat /root/.puppetlabs/token); exec /usr/bin/curl -sk -H "Accept: application/json" -H "X-Authentication: ${token}" https://127.0.0.1:8170/code-manager/v1/deploys/status' >"$status_file"; then
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
      return 0
    fi

    if grep -q "Authentication token has been revoked" "$status_file" 2>/dev/null && (( SECONDS < deadline )); then
      echo "[info] code deploy status attempt ${attempt} is waiting for post-failover auth stability"
      attempt=$((attempt + 1))
      sleep 5
      continue
    fi

    cat "$status_file" >&2
    return 1
  done
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
    if grep -q "Authentication token has been revoked" "$task_output" 2>/dev/null; then
      :
    elif ! grep -q "either disconnected or does not have a connection type" "$task_output"; then
      cat "$task_output" >&2
      return 1
    fi
    if (( SECONDS >= deadline )); then
      cat "$task_output" >&2
      return 1
    fi
    if grep -q "Authentication token has been revoked" "$task_output" 2>/dev/null; then
      echo "[info] task run attempt ${attempt} is waiting for auth stability"
    else
      echo "[info] task run attempt ${attempt} is waiting for PCP/orchestration reconnection"
    fi
    attempt=$((attempt + 1))
    sleep 5
  done
  summarize_job_output "task run completed" "$task_output"

  echo "[check] plan run via ${pod}"
  attempt=1
  until run_plan_check "$pod" "$certname" >"$plan_output"; do
    if grep -q "Authentication token has been revoked" "$plan_output" 2>/dev/null; then
      :
    elif ! grep -q "either disconnected or does not have a connection type" "$plan_output"; then
      cat "$plan_output" >&2
      return 1
    fi
    if (( SECONDS >= deadline )); then
      cat "$plan_output" >&2
      return 1
    fi
    if grep -q "Authentication token has been revoked" "$plan_output" 2>/dev/null; then
      echo "[info] plan run attempt ${attempt} is waiting for auth stability"
    else
      echo "[info] plan run attempt ${attempt} is waiting for PCP/orchestration reconnection"
    fi
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

  echo "[check] agent run via ${compiler_server}"
  run_agent_check

  run_orchestration_check "$pod" "$certname" "$phase"
}

certname="$(agent_certname)"
initial_active="$(wait_for_frontdoor)"
initial_standby="$(pe_service_pods | grep -vx "$initial_active" | head -n1)"

echo "[info] initial active backend: ${initial_active}"
echo "[info] initial standby backend: ${initial_standby}"
run_checks "$initial_active" "$certname" initial
run_rbac_projection_check "$initial_active" "$initial_standby" initial
wait_for_pod_ready "$initial_standby"
run_job_history_sync_check "$initial_active" "$initial_standby" "$certname" initial
run_ca_enrollment_check "$initial_active"

echo "[action] deleting active backend ${initial_active}"
kubectl -n "$namespace" delete pod "$initial_active" --wait=false

new_active="$(wait_for_frontdoor "$initial_active")"
echo "[info] new active backend: ${new_active}"
run_checks "$new_active" "$certname" post-failover
run_ca_revocation_check "$new_active" "$initial_active"

echo "[check] standby re-entry for ${initial_active}"
wait_for_standby_reentry "$initial_active"
echo "[ok] standby re-entry completed"
run_rbac_projection_check "$new_active" "$initial_active" post-failover
run_job_history_sync_check "$new_active" "$initial_active" "$certname" post-failover

echo "[ok] failover validation completed"
