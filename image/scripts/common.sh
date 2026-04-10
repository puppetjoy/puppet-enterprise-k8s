#!/bin/bash
set -euo pipefail

PE_K8S_STATE_DIR="${PE_K8S_STATE_DIR:-/var/lib/pe-k8s}"
PE_K8S_INSTALL_DIR="${PE_K8S_INSTALL_DIR:-${PE_K8S_STATE_DIR}/install}"
PE_K8S_INSTALL_MARKER="${PE_K8S_INSTALL_MARKER:-${PE_K8S_INSTALL_DIR}/install-complete}"
PE_K8S_SYSCONFIG_DIR="${PE_K8S_SYSCONFIG_DIR:-${PE_K8S_STATE_DIR}/sysconfig}"
PE_K8S_WAIT_TIMEOUT_SECONDS="${PE_K8S_WAIT_TIMEOUT_SECONDS:-3600}"
PE_K8S_SKIP_INSTALL_MARKER="${PE_K8S_SKIP_INSTALL_MARKER:-false}"
PE_K8S_PCP_CONTROLLER_LOCAL_HOST="${PE_K8S_PCP_CONTROLLER_LOCAL_HOST:-puppet}"
PE_K8S_SERVICEACCOUNT_DIR="${PE_K8S_SERVICEACCOUNT_DIR:-/var/run/secrets/kubernetes.io/serviceaccount}"

log() {
    printf '[pe-k8s] %s\n' "$*"
}

ensure_dir() {
    mkdir -p "$1"
}

require_file() {
    local path="$1"
    [ -f "${path}" ] || {
        log "Required file is missing: ${path}"
        return 1
    }
}

k8s_api_server() {
    local host="${KUBERNETES_SERVICE_HOST:-}"
    local port="${KUBERNETES_SERVICE_PORT_HTTPS:-${KUBERNETES_SERVICE_PORT:-443}}"

    [ -n "${host}" ] || {
        log "KUBERNETES_SERVICE_HOST is not set"
        return 1
    }

    printf 'https://%s:%s\n' "${host}" "${port}"
}

k8s_serviceaccount_token_path() {
    printf '%s/token\n' "${PE_K8S_SERVICEACCOUNT_DIR}"
}

k8s_serviceaccount_ca_path() {
    printf '%s/ca.crt\n' "${PE_K8S_SERVICEACCOUNT_DIR}"
}

k8s_namespace() {
    if [ -n "${PE_K8S_NAMESPACE:-}" ]; then
        printf '%s\n' "${PE_K8S_NAMESPACE}"
        return 0
    fi

    require_file "${PE_K8S_SERVICEACCOUNT_DIR}/namespace" >/dev/null
    cat "${PE_K8S_SERVICEACCOUNT_DIR}/namespace"
}

k8s_api_get() {
    local path="$1"
    local token_path
    local ca_path
    local token

    token_path="$(k8s_serviceaccount_token_path)"
    ca_path="$(k8s_serviceaccount_ca_path)"

    require_file "${token_path}" >/dev/null
    require_file "${ca_path}" >/dev/null
    token="$(cat "${token_path}")"

    curl -fsS \
        --cacert "${ca_path}" \
        -H "Authorization: Bearer ${token}" \
        "$(k8s_api_server)${path}"
}

job_completion_state() {
    local namespace="$1"
    local job_name="$2"
    local payload

    payload="$(k8s_api_get "/apis/batch/v1/namespaces/${namespace}/jobs/${job_name}")" || return 1

    python3 - "${payload}" <<'PY'
import json
import sys

job = json.loads(sys.argv[1])
status = job.get("status") or {}

for condition in status.get("conditions") or []:
    condition_type = condition.get("type")
    condition_status = condition.get("status")
    if condition_type == "Complete" and condition_status == "True":
        print("complete")
        raise SystemExit(0)
    if condition_type == "Failed" and condition_status == "True":
        print("failed")
        raise SystemExit(0)

if (status.get("succeeded") or 0) > 0:
    print("complete")
else:
    print("pending")
PY
}

wait_for_k8s_job_completion() {
    local job_name="$1"
    local namespace="${2:-}"
    local timeout="${3:-${PE_K8S_WAIT_TIMEOUT_SECONDS}}"
    local deadline
    local state

    [ -n "${job_name}" ] || {
        log "Job name is required"
        return 1
    }

    if [ -z "${namespace}" ]; then
        namespace="$(k8s_namespace)"
    fi

    deadline=$((SECONDS + timeout))
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        state="$(job_completion_state "${namespace}" "${job_name}" || true)"
        case "${state}" in
            complete)
                log "Observed completed Job ${namespace}/${job_name}"
                return 0
                ;;
            failed)
                log "Observed failed Job ${namespace}/${job_name}"
                return 1
                ;;
            pending|"")
                ;;
            *)
                log "Unexpected Job state for ${namespace}/${job_name}: ${state}"
                ;;
        esac
        sleep 5
    done

    log "Timed out waiting for Job ${namespace}/${job_name} to complete"
    return 1
}

hocon_string_setting() {
    local key="$1"
    local path="$2"
    local line

    [ -f "${path}" ] || return 1

    line="$(grep -E "^[[:space:]]*(\"${key}\"|${key})[[:space:]]*=" "${path}" | head -n 1 || true)"
    [ -n "${line}" ] || return 1

    printf '%s\n' "${line}" | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*$//' | sed -E 's/^"(.*)"$/\1/'
}

json_string() {
    python3 - "$1" <<'PY'
import json
import sys

print(json.dumps(sys.argv[1]))
PY
}

remote_pe_service() {
    local service="${1:-${PE_REMOTE_SERVICE:-${PE_COMPILER_PE_SERVICE:-${PE_SIGN_PE_SERVICE:-pe}}}}"
    printf '%s\n' "${service}"
}

wait_for_remote_pe_status() {
    local service="${1:-$(remote_pe_service)}"
    local timeout="${2:-${PE_K8S_WAIT_TIMEOUT_SECONDS}}"
    local path="${3:-/status/v1/services}"
    local deadline

    deadline=$((SECONDS + timeout))
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        if curl -skf "https://${service}:8140${path}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done

    log "Timed out waiting for PE status endpoint on ${service}:8140${path}"
    return 1
}

wait_for_remote_console() {
    local service="${1:-$(remote_pe_service)}"
    local timeout="${2:-${PE_K8S_WAIT_TIMEOUT_SECONDS}}"
    local deadline

    deadline=$((SECONDS + timeout))
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        if curl -sk -o /dev/null -w '%{http_code}' "https://${service}:4433/rbac-api/v1/users" | grep -Eq '^(200|401|403)$'; then
            return 0
        fi
        sleep 5
    done

    log "Timed out waiting for PE console endpoint on ${service}:4433"
    return 1
}

issue_rbac_token() {
    local service="$1"
    local login="$2"
    local password="$3"
    local lifetime="${4:-5m}"
    local label="${5:-pe-k8s-token}"
    local payload response

    payload="$(python3 - "${login}" "${password}" "${lifetime}" "${label}" <<'PY'
import json
import sys

print(json.dumps({
    "login": sys.argv[1],
    "password": sys.argv[2],
    "lifetime": sys.argv[3],
    "label": sys.argv[4],
}))
PY
)"

    response="$(curl -sk \
        -H "Content-Type: application/json" \
        --request POST \
        "https://${service}:4433/rbac-api/v1/auth/token" \
        --data "${payload}" || true)"

    python3 - "${response}" <<'PY'
import json
import sys

try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError:
    raise SystemExit(1)

token = data.get("token") or ""
if not token:
    raise SystemExit(1)

print(token)
PY
}

sync_puppetdb_integration_settings() {
    local puppet_bin=/opt/puppetlabs/bin/puppet

    if [ ! -x "${puppet_bin}" ] || [ ! -f /etc/puppetlabs/puppet/puppet.conf ]; then
        log "Puppet binary or puppet.conf missing; skipping PuppetDB integration sync"
        return 0
    fi

    "${puppet_bin}" config set storeconfigs true --section master
    "${puppet_bin}" config set storeconfigs_backend puppetdb --section master
    "${puppet_bin}" config set reports puppetdb,store --section master
}

copy_exported_sysconfig_into_rootfs() {
    local path
    local target

    ensure_dir /etc/sysconfig
    if [ -d "${PE_K8S_SYSCONFIG_DIR}" ]; then
        find "${PE_K8S_SYSCONFIG_DIR}" -maxdepth 1 -type f -name 'pe-*' -print0 | while IFS= read -r -d '' path; do
            target="/etc/sysconfig/$(basename "${path}")"
            if [ -e "${target}" ]; then
                continue
            fi
            cp -f "${path}" "${target}"
        done
    fi
}

patch_local_pcp_controller_uri() {
    local config_path=/etc/puppetlabs/orchestration-services/conf.d/pcp-broker.conf
    local desired_uri
    local escaped_host

    [ -n "${PE_K8S_PCP_CONTROLLER_LOCAL_HOST}" ] || return 0
    [ -f "${config_path}" ] || return 0

    desired_uri="wss://${PE_K8S_PCP_CONTROLLER_LOCAL_HOST}:8143/server"
    if grep -Fq "\"${desired_uri}\"" "${config_path}"; then
        return 0
    fi

    escaped_host="$(printf '%s' "${PE_K8S_PCP_CONTROLLER_LOCAL_HOST}" | sed 's/[\/&]/\\&/g')"
    sed -i -E "s#wss://[^/\"]+:8143/server#wss://${escaped_host}:8143/server#g" "${config_path}"
    log "Patched PCP controller URI to ${desired_uri}"
}

patch_orchestrator_pcp_broker_allowlist() {
    local config_path=/etc/puppetlabs/orchestration-services/conf.d/auth.conf
    local desired_csv="${PE_K8S_ORCHESTRATOR_PCP_BROKERS_CSV:-}"

    [ -n "${desired_csv}" ] || return 0
    [ -f "${config_path}" ] || return 0

    python3 - "${config_path}" "${desired_csv}" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
desired = []
for entry in sys.argv[2].split(","):
    entry = entry.strip()
    if entry and entry not in desired:
        desired.append(entry)

if not desired:
    raise SystemExit(0)

lines = config_path.read_text(encoding="utf-8").splitlines()
name_line = '          "name": "dispatch: allow pcp-brokers",'

try:
    name_index = next(index for index, line in enumerate(lines) if line == name_line)
except StopIteration as exc:
    raise SystemExit("dispatch: allow pcp-brokers rule not found") from exc

match_request_index = None
for index in range(name_index - 1, -1, -1):
    if lines[index] == '          "match-request": {':
        match_request_index = index
        break

if match_request_index is None:
    raise SystemExit("dispatch: allow pcp-brokers match-request not found")

allow_start = None
for index in range(match_request_index - 1, -1, -1):
    if lines[index] == '          "allow": [':
        allow_start = index
        break

if allow_start is None:
    raise SystemExit("dispatch: allow pcp-brokers allow list not found")

allow_end = None
for index in range(allow_start + 1, match_request_index):
    if lines[index] == '          ],':
        allow_end = index
        break

if allow_end is None:
    raise SystemExit("dispatch: allow pcp-brokers allow list terminator not found")

new_allow_lines = ['          "allow": [']
for index, entry in enumerate(desired):
    suffix = "," if index < len(desired) - 1 else ""
    new_allow_lines.append(f'              {json.dumps(entry)}{suffix}')
new_allow_lines.append('          ],')

updated_lines = lines[:allow_start] + new_allow_lines + lines[allow_end + 1:]

if updated_lines != lines:
    config_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
PY

    log "Patched orchestrator PCP broker allowlist to ${desired_csv}"
}

install_service_control_wrappers() {
    local wrapper=/usr/local/bin/pe-k8s-servicectl
    local path

    for path in \
        /usr/local/bin/systemctl \
        /bin/systemctl \
        /usr/bin/systemctl \
        /sbin/service \
        /usr/sbin/service \
        /sbin/chkconfig \
        /usr/sbin/chkconfig
    do
        ln -sf "${wrapper}" "${path}"
    done
}

maintain_service_control_wrappers() {
    while true; do
        install_service_control_wrappers
        sleep 1
    done
}

export_runtime_rootfs_artifacts() {
    local path

    ensure_dir "${PE_K8S_SYSCONFIG_DIR}"

    for path in /etc/sysconfig/pe-*; do
        [ -f "${path}" ] || continue
        cp -f "${path}" "${PE_K8S_SYSCONFIG_DIR}/"
    done

    if [ -f /etc/sysconfig/pe-pgsql ]; then
        cp -f /etc/sysconfig/pe-pgsql "${PE_K8S_SYSCONFIG_DIR}/"
    fi
}

ensure_pe_build_metadata() {
    local pe_build="${PE_BUILD:-}"
    local rpm_version=""

    if [ -z "${pe_build}" ] && command -v rpm >/dev/null 2>&1; then
        rpm_version="$(rpm -q pe-puppet-enterprise-release --queryformat '%{VERSION}\n' 2>/dev/null || true)"
        if [ -n "${rpm_version}" ]; then
            pe_build="$(printf '%s' "${rpm_version}" | sed -E 's/\.0$//')"
        fi
    fi

    if [ -z "${pe_build}" ] && [ -n "${PE_VERSION:-}" ]; then
        pe_build="${PE_VERSION}"
    fi

    [ -n "${pe_build}" ] || {
        log "Unable to determine PE build metadata"
        return 1
    }

    ensure_dir /opt/puppetlabs/server
    printf '%s\n' "${pe_build}" > /opt/puppetlabs/server/pe_build
}

normalize_dns_alt_names() {
    tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | sed '/^$/d' | sort -u
}

current_dns_alt_names() {
    local cert_path="$1"

    openssl x509 -in "${cert_path}" -noout -ext subjectAltName 2>/dev/null | \
        tr ',' '\n' | sed -n 's/^[[:space:]]*DNS://p' | sed 's/[[:space:]]*$//' | sed '/^$/d' | sort -u
}

desired_dns_alt_names() {
    local dns_alt_names="$1"

    printf '%s\n' "${dns_alt_names}" | normalize_dns_alt_names
}

cert_matches_desired_dns_alt_names() {
    local cert_path="$1"
    local dns_alt_names="$2"
    local current desired

    [ -f "${cert_path}" ] || return 1

    current="$(current_dns_alt_names "${cert_path}")"
    desired="$(desired_dns_alt_names "${dns_alt_names}")"
    [ "${current}" = "${desired}" ]
}

remove_pe_host_identity_material() {
    local certname="$1"

    rm -f \
        "/etc/puppetlabs/puppet/ssl/certs/${certname}.pem" \
        "/etc/puppetlabs/puppet/ssl/private_keys/${certname}.pem" \
        "/etc/puppetlabs/puppet/ssl/public_keys/${certname}.pem" \
        "/etc/puppetlabs/puppet/ssl/certificate_requests/${certname}.pem" \
        "/etc/puppetlabs/puppetserver/ca/signed/${certname}.pem" \
        "/etc/puppetlabs/puppetserver/ca/requests/${certname}.pem"
}

latest_backup_dir() {
    local base_dir="$1"

    ls -dt "${base_dir}"_bak_* 2>/dev/null | head -n 1 || true
}

pe_service_ssl_targets() {
    cat <<'EOF'
/etc/puppetlabs/puppetdb/ssl:/etc/puppetlabs/puppetdb/ssl
/etc/puppetlabs/orchestration-services/ssl:/etc/puppetlabs/orchestration-services/ssl
/opt/puppetlabs/server/data/console-services/certs:/opt/puppetlabs/server/data/console-services/certs
/opt/puppetlabs/server/data/host-action-collector/ssl:/opt/puppetlabs/server/data/host-action-collector/ssl
/opt/puppetlabs/server/data/infra-assistant/ssl:/opt/puppetlabs/server/data/infra-assistant/ssl
/opt/puppetlabs/server/data/patching-service/ssl:/opt/puppetlabs/server/data/patching-service/ssl
/opt/puppetlabs/server/data/workflow-service/ssl:/opt/puppetlabs/server/data/workflow-service/ssl
/etc/puppetlabs/bolt-server/ssl:
/etc/puppetlabs/ace-server/ssl:
EOF
}

sync_pe_service_ssl_material() {
    local cert_path="$1"
    local key_path="$2"
    local pk8_path
    local dir
    local backup_base
    local backup_dir

    require_file "${cert_path}"
    require_file "${key_path}"

    pk8_path="$(mktemp)"
    openssl pkcs8 -topk8 -inform PEM -outform DER -nocrypt -in "${key_path}" -out "${pk8_path}"

    while IFS=: read -r dir backup_base; do
        [ -n "${dir}" ] || continue

        ensure_dir "${dir}"

        cp -f "${cert_path}" "${dir}/pe.cert.pem"
        cp -f "${key_path}" "${dir}/pe.private_key.pem"
        cp -f "${pk8_path}" "${dir}/pe.private_key.pk8"

        backup_dir=""
        if [ -n "${backup_base}" ]; then
            backup_dir="$(latest_backup_dir "${backup_base}")"
        fi

        if [ -n "${backup_dir}" ] && [ -f "${backup_dir}/pe.cert.pem" ]; then
            chown --reference="${backup_dir}/pe.cert.pem" "${dir}/pe.cert.pem"
            chmod --reference="${backup_dir}/pe.cert.pem" "${dir}/pe.cert.pem"
        else
            chown --reference="${dir}" "${dir}/pe.cert.pem"
            chmod 0444 "${dir}/pe.cert.pem"
        fi

        if [ -n "${backup_dir}" ] && [ -f "${backup_dir}/pe.private_key.pem" ]; then
            chown --reference="${backup_dir}/pe.private_key.pem" "${dir}/pe.private_key.pem"
            chmod --reference="${backup_dir}/pe.private_key.pem" "${dir}/pe.private_key.pem"
        else
            chown --reference="${dir}" "${dir}/pe.private_key.pem"
            chmod 0400 "${dir}/pe.private_key.pem"
        fi

        if [ -n "${backup_dir}" ] && [ -f "${backup_dir}/pe.private_key.pk8" ]; then
            chown --reference="${backup_dir}/pe.private_key.pk8" "${dir}/pe.private_key.pk8"
            chmod --reference="${backup_dir}/pe.private_key.pk8" "${dir}/pe.private_key.pk8"
        else
            chown --reference="${dir}" "${dir}/pe.private_key.pk8"
            chmod 0400 "${dir}/pe.private_key.pk8"
        fi
    done < <(pe_service_ssl_targets)

    rm -f "${pk8_path}"
}

repair_pe_service_ssl_material() {
    local certname="$1"

    sync_pe_service_ssl_material \
        "/etc/puppetlabs/puppet/ssl/certs/${certname}.pem" \
        "/etc/puppetlabs/puppet/ssl/private_keys/${certname}.pem"
}

patch_nginx_ingress_redirects() {
    local path

    path=/etc/puppetlabs/nginx/conf.d/proxy.conf
    if [ -f "${path}" ]; then
        sed -i 's/ ipv6only=off//g' "${path}"
    fi

    path=/etc/puppetlabs/nginx/conf.d/http_redirect.conf
    if [ -f "${path}" ]; then
        cat > "${path}" <<'EOF'
map $http_x_forwarded_proto $pe_ingress_https {
  default 0;
  https 1;
}

server {
  server_name _;
  listen 0.0.0.0:80;

  if ($pe_ingress_https != 1) {
    return 301 https://$http_host$request_uri;
  }

  location / {
    proxy_pass http://localhost:4430;
    proxy_redirect http://localhost:4430 /;
    proxy_read_timeout 120;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
  }

  location /saml {
    proxy_pass https://0.0.0.0:4431;
    proxy_redirect https://0.0.0.0:4431 /;
    proxy_read_timeout 120;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
  }
}
EOF
    fi
}

user_home() {
    local user="$1"
    getent passwd "${user}" | cut -d: -f6
}

prepare_user_env() {
    local user="$1"
    local home

    home="$(user_home "${user}")"
    [ -n "${home}" ] || home="/"

    export HOME="${home}"
    export USER="${user}"
    export LOGNAME="${user}"
}

wait_for_install_marker() {
    local elapsed=0
    local interval=5

    case "${PE_K8S_SKIP_INSTALL_MARKER}" in
        true|TRUE|1|yes|YES|on|ON)
            return 0
            ;;
    esac

    while [ ! -f "${PE_K8S_INSTALL_MARKER}" ]; do
        if [ "${elapsed}" -ge "${PE_K8S_WAIT_TIMEOUT_SECONDS}" ]; then
            log "Timed out waiting for install marker ${PE_K8S_INSTALL_MARKER}"
            return 1
        fi
        sleep "${interval}"
        elapsed=$((elapsed + interval))
    done
}

exec_as_user() {
    local user="$1"
    shift

    if [ "$(id -u)" -eq 0 ]; then
        prepare_user_env "${user}"
        exec runuser --preserve-environment -u "${user}" -- "$@"
    fi

    exec "$@"
}
