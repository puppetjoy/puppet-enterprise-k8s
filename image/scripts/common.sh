#!/bin/bash
set -euo pipefail

PE_K8S_STATE_DIR="${PE_K8S_STATE_DIR:-/var/lib/pe-k8s}"
PE_K8S_INSTALL_DIR="${PE_K8S_INSTALL_DIR:-${PE_K8S_STATE_DIR}/install}"
PE_K8S_INSTALL_MARKER="${PE_K8S_INSTALL_MARKER:-${PE_K8S_INSTALL_DIR}/install-complete}"
PE_K8S_SYSCONFIG_DIR="${PE_K8S_SYSCONFIG_DIR:-${PE_K8S_STATE_DIR}/sysconfig}"
PE_K8S_WAIT_TIMEOUT_SECONDS="${PE_K8S_WAIT_TIMEOUT_SECONDS:-3600}"
PE_K8S_SKIP_INSTALL_MARKER="${PE_K8S_SKIP_INSTALL_MARKER:-false}"
PE_K8S_ORCHESTRATION_SERVICE_HOST="${PE_K8S_ORCHESTRATION_SERVICE_HOST:-}"
PE_K8S_PCP_CONTROLLER_LOCAL_HOST="${PE_K8S_PCP_CONTROLLER_LOCAL_HOST:-${PE_K8S_ORCHESTRATION_SERVICE_HOST:-pe}}"
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

k8s_api_get_optional() {
    local path="$1"
    local token_path
    local ca_path
    local token
    local response_path
    local status_code

    token_path="$(k8s_serviceaccount_token_path)"
    ca_path="$(k8s_serviceaccount_ca_path)"

    require_file "${token_path}" >/dev/null
    require_file "${ca_path}" >/dev/null
    token="$(cat "${token_path}")"
    response_path="$(mktemp)"

    status_code="$(
        curl -sS -o "${response_path}" -w '%{http_code}' \
            --cacert "${ca_path}" \
            -H "Authorization: Bearer ${token}" \
            "$(k8s_api_server)${path}"
    )"

    case "${status_code}" in
        200)
            cat "${response_path}"
            rm -f "${response_path}"
            return 0
            ;;
        404)
            rm -f "${response_path}"
            return 1
            ;;
        *)
            cat "${response_path}" >&2 || true
            rm -f "${response_path}"
            return 1
            ;;
    esac
}

remote_pe_headless_service() {
    local service="${1:-$(remote_pe_service)}"

    if [ -n "${PE_REMOTE_HEADLESS_SERVICE:-}" ]; then
        printf '%s\n' "${PE_REMOTE_HEADLESS_SERVICE}"
        return 0
    fi

    printf '%s-headless\n' "${service}"
}

select_remote_pe_endpoint() {
    local service="${1:-$(remote_pe_service)}"
    local namespace="${2:-$(k8s_namespace)}"
    local headless_service="${3:-$(remote_pe_headless_service "${service}")}"
    local payload

    payload="$(k8s_api_get_optional "/api/v1/namespaces/${namespace}/endpoints/${service}" 2>/dev/null)" || return 1

    python3 - "${payload}" "${namespace}" "${headless_service}" <<'PY'
import json
import sys

endpoints = json.loads(sys.argv[1])
namespace = sys.argv[2]
headless_service = sys.argv[3]
hosts = []

for subset in endpoints.get("subsets") or []:
    for address in subset.get("addresses") or []:
        target_ref = address.get("targetRef") or {}
        pod_name = (target_ref.get("name") or address.get("hostname") or "").strip()
        if not pod_name:
            continue
        host = f"{pod_name}.{headless_service}.{namespace}.svc.cluster.local"
        if host not in hosts:
            hosts.append(host)

if not hosts:
    raise SystemExit(1)

print(sorted(hosts)[0])
PY
}

k8s_secret_data_field() {
    local namespace="$1"
    local secret_name="$2"
    local field_name="$3"
    local payload

    payload="$(k8s_api_get_optional "/api/v1/namespaces/${namespace}/secrets/${secret_name}")" || return 1

    python3 - "${payload}" "${field_name}" <<'PY'
import base64
import json
import sys

secret = json.loads(sys.argv[1])
field_name = sys.argv[2]
data = (secret.get("data") or {}).get(field_name, "")
if not data:
    raise SystemExit(1)

sys.stdout.write(base64.b64decode(data.encode("ascii")).decode("utf-8"))
PY
}

select_first_present_secret() {
    local namespace="$1"
    shift

    local secret_name
    for secret_name in "$@"; do
        [ -n "${secret_name}" ] || continue
        if k8s_api_get_optional "/api/v1/namespaces/${namespace}/secrets/${secret_name}" >/dev/null 2>&1; then
            printf '%s\n' "${secret_name}"
            return 0
        fi
    done

    return 1
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

write_first_pem_block() {
    local source_path="$1"
    local label="$2"
    local output_path="$3"

    python3 - "${source_path}" "${label}" "${output_path}" <<'PY'
from pathlib import Path
import re
import sys

source_path = Path(sys.argv[1])
label = sys.argv[2]
output_path = Path(sys.argv[3])
text = source_path.read_text(encoding="utf-8")
pattern = re.compile(
    rf"-----BEGIN {re.escape(label)}-----\s*.*?-----END {re.escape(label)}-----\s*",
    re.S,
)
match = pattern.search(text)
if match is None:
    raise SystemExit(1)
output_path.write_text(match.group(0).strip() + "\n", encoding="utf-8")
PY
}

control_plane_hostcrl_path() {
    printf '%s\n' "${PE_CONTROL_PLANE_HOSTCRL_PATH:-/etc/puppetlabs/puppet/ssl/crl-chain.pem}"
}

urlencode() {
    python3 - "$1" <<'PY'
from urllib.parse import quote
import sys

print(quote(sys.argv[1], safe=""))
PY
}

current_pod_name() {
    printf '%s\n' "${PE_CONTROL_PLANE_POD_NAME:-${HOSTNAME:-}}"
}

current_release_name() {
    local namespace="${1:-$(k8s_namespace)}"
    local pod_name="${2:-$(current_pod_name)}"
    local payload

    if [ -n "${PE_K8S_RELEASE_NAME:-}" ]; then
        printf '%s\n' "${PE_K8S_RELEASE_NAME}"
        return 0
    fi

    payload="$(k8s_api_get "/api/v1/namespaces/${namespace}/pods/${pod_name}")" || return 1

    python3 - "${payload}" <<'PY'
import json
import sys

pod = json.loads(sys.argv[1])
labels = (pod.get("metadata") or {}).get("labels") or {}
release = (labels.get("app.kubernetes.io/instance") or "").strip()
if not release:
    raise SystemExit(1)
print(release)
PY
}

sync_control_plane_services_conf() {
    local enabled="${PE_K8S_SERVICES_CONF_SYNC_ENABLED:-false}"
    local services_conf_path="${PE_K8S_SERVICES_CONF_PATH:-/etc/puppetlabs/client-tools/services.conf}"
    local namespace pod_name release control_plane_headless_service compiler_headless_service
    local control_plane_selector compiler_selector
    local control_plane_json compiler_json

    [ "${enabled}" = "true" ] || return 0

    namespace="$(k8s_namespace)"
    pod_name="$(current_pod_name)"
    release="$(current_release_name "${namespace}" "${pod_name}")" || {
        log "Unable to determine Helm release name for services.conf sync"
        return 1
    }

    control_plane_headless_service="${PE_K8S_CONTROL_PLANE_HEADLESS_SERVICE:-${release}-headless}"
    compiler_headless_service="${PE_K8S_COMPILER_HEADLESS_SERVICE:-${release}-compiler-headless}"

    control_plane_selector="$(urlencode "app.kubernetes.io/instance=${release},app.kubernetes.io/component=pe-services")"
    compiler_selector="$(urlencode "app.kubernetes.io/instance=${release},app.kubernetes.io/component=compiler")"

    control_plane_json="$(mktemp)"
    compiler_json="$(mktemp)"
    trap 'rm -f "${control_plane_json}" "${compiler_json}"' RETURN

    k8s_api_get "/api/v1/namespaces/${namespace}/pods?labelSelector=${control_plane_selector}" > "${control_plane_json}"
    if ! k8s_api_get_optional "/api/v1/namespaces/${namespace}/pods?labelSelector=${compiler_selector}" > "${compiler_json}"; then
        printf '{"items":[]}\n' > "${compiler_json}"
    fi

    local sync_result

    sync_result="$(
        python3 - \
        "${services_conf_path}" \
        "${namespace}" \
        "${control_plane_headless_service}" \
        "${compiler_headless_service}" \
        "${control_plane_json}" \
        "${compiler_json}" <<'PY'
from pathlib import Path
import json
import sys

services_conf_path = Path(sys.argv[1])
namespace = sys.argv[2]
control_plane_headless_service = sys.argv[3]
compiler_headless_service = sys.argv[4]
control_plane_json_path = Path(sys.argv[5])
compiler_json_path = Path(sys.argv[6])

control_plane_payload = json.loads(control_plane_json_path.read_text(encoding="utf-8"))
compiler_payload = json.loads(compiler_json_path.read_text(encoding="utf-8"))


def sorted_running_pods(payload):
    pods = []
    for item in payload.get("items") or []:
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        if metadata.get("deletionTimestamp"):
            continue
        if status.get("phase") != "Running":
            continue
        pods.append(item)

    def sort_key(item):
        metadata = item.get("metadata") or {}
        labels = metadata.get("labels") or {}
        index = labels.get("apps.kubernetes.io/pod-index")
        try:
            return (0, int(index))
        except (TypeError, ValueError):
            return (1, metadata.get("name") or "")

    return sorted(pods, key=sort_key)


def certname_for_pod(pod_name, headless_service_name):
    return f"{pod_name}.{headless_service_name}.{namespace}.svc.cluster.local"


def pod_name(item):
    return ((item.get("metadata") or {}).get("name") or "").strip()


def active_primary_name(control_plane_pods):
    for item in control_plane_pods:
        labels = (item.get("metadata") or {}).get("labels") or {}
        if (labels.get("pe-k8s.puppet.com/frontdoor-active") or "").strip().lower() == "true":
            return pod_name(item)

    for item in control_plane_pods:
        annotations = (item.get("metadata") or {}).get("annotations") or {}
        if (annotations.get("pe-k8s.puppet.com/frontdoor-eligible") or "").strip().lower() == "true":
            return pod_name(item)

    if control_plane_pods:
        return pod_name(control_plane_pods[0])

    return ""


def service_url(protocol, certname, port, prefix):
    prefix = (prefix or "").strip("/")
    if prefix:
        return f"{protocol}://{certname}:{port}/{prefix}"
    return f"{protocol}://{certname}:{port}/"


def pod_ip(item):
    return ((item.get("status") or {}).get("podIP") or "").strip()


def postgresql_service(certname, primary, status_host=None):
    status_target = status_host or certname
    return {
        "type": "postgresql",
        "url": service_url("postgresql", certname, 5432, ""),
        "server": certname,
        "port": 5432,
        "prefix": "",
        # PE's PostgreSQL status helper treats nodes named as primary/replica as
        # pglogical participants and emits replication warnings. Our control-plane
        # PostgreSQL instances are independent, so use the pod IP for the status
        # transport to force standalone liveness checks while preserving the real
        # certname for display and service identity.
        "status_url": service_url("postgresql", status_target, 5432, ""),
        "status_prefix": "",
        "status_key": "pe-postgresql",
        "node_certname": certname,
        "display_name": "PostgreSQL",
        "primary": primary,
        "database_configs": {
            "activity": {"database": "pe-activity", "user": "pe-activity"},
            "classifier": {"database": "pe-classifier", "user": "pe-classifier"},
            "inventory": {"database": "pe-inventory", "user": "pe-inventory"},
            "orchestrator": {"database": "pe-orchestrator", "user": "pe-orchestrator"},
            "rbac": {"database": "pe-rbac", "user": "pe-rbac"},
            "pe-hac": {"database": "pe-hac", "user": "pe-hac"},
            "pe-patching": {"database": "pe-patching", "user": "pe-patching"},
            "pe-infra-assistant": {"database": "pe-infra-assistant", "user": "pe-infra-assistant"},
            "pe-workflow": {"database": "pe-workflow", "user": "pe-workflow"},
        },
        "certs_dir": "/opt/puppetlabs/server/data/postgresql/14/data/certs",
    }


CONTROL_PLANE_SERVICE_DEFS = [
    {"type": "master", "port": 8140, "prefix": "", "status_key": "pe-master", "display_name": "Puppet Server"},
    {"type": "file-sync-storage", "port": 8140, "prefix": "", "status_key": "file-sync-storage-service", "display_name": "File Sync Storage Service"},
    {"type": "file-sync-client", "port": 8140, "prefix": "", "status_key": "file-sync-client-service", "display_name": "File Sync Client Service"},
    {"type": "code-manager", "port": 8170, "prefix": "", "status_port": 8140, "status_key": "code-manager-service", "display_name": "Code Manager"},
    {"type": "puppetdb", "port": 8081, "prefix": "pdb", "status_key": "puppetdb-status", "display_name": "PuppetDB"},
    {"type": "orchestrator", "port": 8143, "prefix": "orchestrator", "status_key": "orchestrator-service", "display_name": "Orchestrator"},
    {"type": "pcp-broker", "port": 8142, "prefix": "pcp", "status_port": 8143, "status_key": "broker-service", "display_name": "PCP Broker", "protocol": "wss", "status_protocol": "https"},
    {"type": "pcp-broker", "port": 8142, "prefix": "pcp2", "status_port": 8143, "status_key": "broker-service", "display_name": "PCP Broker v2", "protocol": "wss", "status_protocol": "https"},
    {"type": "classifier", "port": 4433, "prefix": "classifier-api", "status_key": "classifier-service", "display_name": "Classifier"},
    {"type": "rbac", "port": 4433, "prefix": "rbac-api", "status_key": "rbac-service", "display_name": "RBAC"},
    {"type": "activity", "port": 4433, "prefix": "activity-api", "status_key": "activity-service", "display_name": "Activity Service"},
    {"type": "pe-host-action-collector", "port": 8147, "prefix": "", "status_key": "pe-host-action-collector", "display_name": "PE Host Action Collector Service"},
    {"type": "bolt", "port": 62658, "prefix": "", "status_prefix": "admin/status", "status_key": "bolt-server", "display_name": "Bolt Service"},
    {"type": "ace", "port": 44633, "prefix": "", "status_prefix": "admin/status", "status_key": "ace-server", "display_name": "Agentless Catalog Executor Service"},
]

COMPILER_SERVICE_DEFS = [
    {"type": "master", "port": 8140, "prefix": "", "status_key": "pe-master", "display_name": "Puppet Server"},
    {"type": "file-sync-client", "port": 8140, "prefix": "", "status_key": "file-sync-client-service", "display_name": "File Sync Client Service"},
    {"type": "puppetdb", "port": 8081, "prefix": "pdb", "status_key": "puppetdb-status", "display_name": "PuppetDB"},
    {"type": "pcp-broker", "port": 8142, "prefix": "pcp", "status_port": 8140, "status_key": "broker-service", "display_name": "PCP Broker", "protocol": "wss", "status_protocol": "https"},
    {"type": "pcp-broker", "port": 8142, "prefix": "pcp2", "status_port": 8140, "status_key": "broker-service", "display_name": "PCP Broker v2", "protocol": "wss", "status_protocol": "https"},
]


def service_entry(certname, primary, definition):
    protocol = definition.get("protocol") or "https"
    status_protocol = definition.get("status_protocol") or protocol
    port = int(definition["port"])
    status_port = int(definition.get("status_port") or port)
    prefix = definition.get("prefix") or ""
    status_prefix = definition.get("status_prefix")
    if status_prefix is None:
        status_prefix = "status/v1/services"

    return {
        "type": definition["type"],
        "url": service_url(protocol, certname, port, prefix),
        "server": certname,
        "port": port,
        "prefix": prefix,
        "status_url": service_url(status_protocol, certname, status_port, status_prefix),
        "status_prefix": status_prefix,
        "status_key": definition["status_key"],
        "node_certname": certname,
        "display_name": definition["display_name"],
        "primary": primary,
    }


control_plane_pods = sorted_running_pods(control_plane_payload)
compiler_pods = sorted_running_pods(compiler_payload)

if not control_plane_pods:
    raise SystemExit("no running control-plane pods found for services.conf sync")

primary_name = active_primary_name(control_plane_pods)

nodes = []
services = []

for item in control_plane_pods:
    name = pod_name(item)
    certname = certname_for_pod(name, control_plane_headless_service)
    status_host = pod_ip(item) or certname
    primary = name == primary_name
    nodes.append(
        {
            "role": "primary_master" if primary else "primary_master_replica",
            "display_name": "Primary" if primary else "Replica",
            "certname": certname,
            "order": 1 if primary else 2,
        }
    )
    services.append(postgresql_service(certname, primary, status_host=status_host))
    for definition in CONTROL_PLANE_SERVICE_DEFS:
        services.append(service_entry(certname, primary, definition))

for item in compiler_pods:
    name = pod_name(item)
    certname = certname_for_pod(name, compiler_headless_service)
    nodes.append(
        {
            "role": "compiler",
            "display_name": "Compiler",
            "certname": certname,
            "order": 4,
        }
    )
    services.append(postgresql_service(certname, False))
    for definition in COMPILER_SERVICE_DEFS:
        services.append(service_entry(certname, False, definition))

document = {
    "services": services,
    "nodes": nodes,
    "certs": {
        "ca-cert": "/etc/puppetlabs/puppet/ssl/certs/ca.pem",
    },
}

rendered = json.dumps(document, indent=2, sort_keys=False) + "\n"
current = services_conf_path.read_text(encoding="utf-8") if services_conf_path.exists() else ""

if current == rendered:
    print("unchanged")
else:
    services_conf_path.write_text(rendered, encoding="utf-8")
    print("updated")
PY
    )"

    chown root:root "${services_conf_path}"
    chmod 0444 "${services_conf_path}"

    if [ "${sync_result}" = "updated" ]; then
        log "Updated ${services_conf_path} from Kubernetes topology"
    fi

    trap - RETURN
    rm -f "${control_plane_json}" "${compiler_json}"
    return 0
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

sync_relay_puppetdb_command_submission_settings() {
    local puppet_conf_path=/etc/puppetlabs/puppet/puppet.conf
    local puppetdb_conf_path=/etc/puppetlabs/puppet/puppetdb.conf
    local relay_port="${PE_K8S_RELAY_COMMAND_PROXY_PORT:-18081}"

    if [ "${PE_K8S_RELAY_COMMAND_PROXY_ENABLED:-false}" != "true" ]; then
        return 0
    fi

    if [ ! -f "${puppet_conf_path}" ]; then
        log "puppet.conf missing; skipping Relay PuppetDB command submission sync"
        return 0
    fi

    python3 - "${puppet_conf_path}" "${puppetdb_conf_path}" "${relay_port}" <<'PY'
from pathlib import Path
import re
import sys

puppet_conf_path = Path(sys.argv[1])
puppetdb_conf_path = Path(sys.argv[2])
relay_port = sys.argv[3]

certname = ""
for line in puppet_conf_path.read_text(encoding="utf-8").splitlines():
    match = re.match(r"^\s*certname\s*=\s*(\S+)\s*$", line)
    if match:
        certname = match.group(1)
        break

if not certname:
    raise SystemExit(f"certname not found in {puppet_conf_path}")

existing = {}
if puppetdb_conf_path.exists():
    current_section = ""
    for line in puppetdb_conf_path.read_text(encoding="utf-8").splitlines():
        section_match = re.match(r"^\s*\[(.+?)\]\s*$", line)
        if section_match:
            current_section = section_match.group(1).strip()
            continue
        if current_section != "main":
            continue
        setting_match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if setting_match:
            existing[setting_match.group(1)] = setting_match.group(2)

ordered_settings = [
    "server_urls",
    "submit_only_server_urls",
    "command_broadcast",
    "include_unchanged_resources",
    "include_catalog_edges",
    "soft_write_failure",
    "sticky_read_failover",
]
defaults = {
    "command_broadcast": "true",
    "include_unchanged_resources": "true",
    "include_catalog_edges": "false",
    "soft_write_failure": "false",
    "sticky_read_failover": "true",
}

existing["server_urls"] = f"https://{certname}:8081"
existing["submit_only_server_urls"] = f"https://{certname}:{relay_port}"
existing["command_broadcast"] = "true"

lines = ["[main]"]
for setting in ordered_settings:
    value = existing.get(setting, defaults.get(setting, ""))
    if value:
        lines.append(f"{setting} = {value}")

for setting in sorted(existing):
    if setting in ordered_settings:
        continue
    value = existing[setting]
    if value:
        lines.append(f"{setting} = {value}")

puppetdb_conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(certname)
PY

    log "Configured Relay command submission through ${puppetdb_conf_path}"
}

sync_relay_code_manager_post_environment_hooks() {
    local hook_enabled="${PE_K8S_CODE_DEPLOY_HOOK_ENABLED:-false}"
    local hook_conf_path=/etc/puppetlabs/puppetserver/conf.d/pe-k8s-code-deploy-hooks.conf
    local hook_url="${PE_K8S_CODE_DEPLOY_HOOK_URL:-}"
    local use_client_ssl="${PE_K8S_CODE_DEPLOY_HOOK_USE_CLIENT_SSL:-true}"

    if [ "${hook_enabled}" != "true" ]; then
        rm -f "${hook_conf_path}"
        return 0
    fi

    [ -n "${hook_url}" ] || {
        log "Code deploy hook URL is empty; refusing to configure Code Manager hooks"
        return 1
    }

    python3 - "${hook_conf_path}" "${hook_url}" "${use_client_ssl}" <<'PY'
from pathlib import Path
import json
import sys

hook_conf_path = Path(sys.argv[1])
hook_url = sys.argv[2]
use_client_ssl = sys.argv[3].strip().lower() in {"1", "true", "yes", "on"}

content = "\n".join([
    "code-manager: {",
    "  hooks: {",
    "    post-environment: [",
    "      {",
    f"        url: {json.dumps(hook_url)}",
    f"        use-client-ssl: {'true' if use_client_ssl else 'false'}",
    "      }",
    "    ]",
    "  }",
    "}",
    "",
])

hook_conf_path.write_text(content, encoding="utf-8")
PY

    log "Configured Code Manager post-environment hooks in ${hook_conf_path}"
}

sync_compiler_file_sync_service_urls() {
    local file_sync_conf_path=/etc/puppetlabs/puppetserver/conf.d/file-sync.conf
    local file_sync_service="${PE_K8S_COMPILER_FILE_SYNC_SERVICE:-}"

    [ -n "${file_sync_service}" ] || return 0

    if [ ! -f "${file_sync_conf_path}" ]; then
        log "file-sync.conf missing; skipping compiler file sync service sync"
        return 0
    fi

    python3 - "${file_sync_conf_path}" "${file_sync_service}" <<'PY'
from pathlib import Path
import re
import sys

file_sync_conf_path = Path(sys.argv[1])
file_sync_service = sys.argv[2].strip()
content = file_sync_conf_path.read_text(encoding="utf-8")

patterns = {
    r'(server-api-url:\s*")[^"]+(")': rf'\1https://{file_sync_service}:8140/file-sync/v1\2',
    r'(server-repo-url:\s*")[^"]+(")': rf'\1https://{file_sync_service}:8140/file-sync-git\2',
}

updated = content
for pattern, replacement in patterns.items():
    updated, count = re.subn(pattern, replacement, updated, count=1)
    if count != 1:
        raise SystemExit(f"unable to update {pattern} in {file_sync_conf_path}")

if updated != content:
    file_sync_conf_path.write_text(updated, encoding="utf-8")
PY

    log "Configured compiler file sync service through ${file_sync_conf_path}"
}

sync_orchestration_service_urls() {
    local orchestration_service="${PE_K8S_ORCHESTRATION_SERVICE_HOST:-}"
    local console_conf_path=/etc/puppetlabs/console-services/conf.d/console.conf
    local client_orchestrator_conf_path=/etc/puppetlabs/client-tools/orchestrator.conf
    local host_action_conf_path=/etc/puppetlabs/host-action-collector/conf.d/pe-host-action-collector.conf

    [ -n "${orchestration_service}" ] || return 0

    if [ -f "${client_orchestrator_conf_path}" ]; then
        python3 - "${client_orchestrator_conf_path}" "${orchestration_service}" <<'PY'
from pathlib import Path
import json
import sys

config_path = Path(sys.argv[1])
service_host = sys.argv[2].strip()
data = json.loads(config_path.read_text(encoding="utf-8"))
options = data.setdefault("options", {})
desired = f"https://{service_host}:8143"
if options.get("service-url") != desired:
    options["service-url"] = desired
    config_path.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
PY
        log "Configured client-tools orchestrator service through ${client_orchestrator_conf_path}"
    fi

    if [ -f "${console_conf_path}" ]; then
        python3 - "${console_conf_path}" "${orchestration_service}" <<'PY'
from pathlib import Path
import re
import sys

config_path = Path(sys.argv[1])
service_host = sys.argv[2].strip()
text = config_path.read_text(encoding="utf-8")
patterns = {
    r'(orchestrator-server:\s*")[^"]+(")': rf'\1https://{service_host}:8143/orchestrator\2',
    r'(inventory-server:\s*")[^"]+(")': rf'\1https://{service_host}:8143/inventory\2',
}
updated = text
for pattern, replacement in patterns.items():
    updated, count = re.subn(pattern, replacement, updated, count=1)
    if count != 1:
        raise SystemExit(f"unable to update {pattern} in {config_path}")
if updated != text:
    config_path.write_text(updated, encoding="utf-8")
PY
        log "Configured console orchestration endpoints through ${console_conf_path}"
    fi

    if [ -f "${host_action_conf_path}" ]; then
        python3 - "${host_action_conf_path}" "${orchestration_service}" <<'PY'
from pathlib import Path
import re
import sys

config_path = Path(sys.argv[1])
service_host = sys.argv[2].strip()
text = config_path.read_text(encoding="utf-8")
updated, count = re.subn(
    r'(inventory-url:\s*")[^"]+(")',
    rf'\1https://{service_host}:8143/inventory\2',
    text,
    count=1,
)
if count != 1:
    raise SystemExit(f"unable to update inventory-url in {config_path}")
if updated != text:
    config_path.write_text(updated, encoding="utf-8")
PY
        log "Configured host-action inventory endpoint through ${host_action_conf_path}"
    fi
}

sync_console_service_alert_config() {
    local enabled="${PE_K8S_CONSOLE_SERVICE_ALERT_SYNC_ENABLED:-false}"
    local console_conf_path="${PE_K8S_CONSOLE_CONF_PATH:-/etc/puppetlabs/console-services/conf.d/console.conf}"
    local namespace pod_name release control_plane_headless_service compiler_headless_service
    local control_plane_selector compiler_selector
    local control_plane_json compiler_json

    [ "${enabled}" = "true" ] || return 0

    if [ ! -f "${console_conf_path}" ]; then
        log "console.conf missing; skipping console service-alert sync"
        return 0
    fi

    namespace="$(k8s_namespace)"
    pod_name="$(current_pod_name)"
    release="$(current_release_name "${namespace}" "${pod_name}")" || {
        log "Unable to determine Helm release name for console service-alert sync"
        return 1
    }

    control_plane_headless_service="${PE_K8S_CONTROL_PLANE_HEADLESS_SERVICE:-${release}-headless}"
    compiler_headless_service="${PE_K8S_COMPILER_HEADLESS_SERVICE:-${release}-compiler-headless}"

    control_plane_selector="$(urlencode "app.kubernetes.io/instance=${release},app.kubernetes.io/component=pe-services")"
    compiler_selector="$(urlencode "app.kubernetes.io/instance=${release},app.kubernetes.io/component=compiler")"

    control_plane_json="$(mktemp)"
    compiler_json="$(mktemp)"
    trap 'rm -f "${control_plane_json}" "${compiler_json}"' RETURN

    k8s_api_get "/api/v1/namespaces/${namespace}/pods?labelSelector=${control_plane_selector}" > "${control_plane_json}"
    if ! k8s_api_get_optional "/api/v1/namespaces/${namespace}/pods?labelSelector=${compiler_selector}" > "${compiler_json}"; then
        printf '{"items":[]}\n' > "${compiler_json}"
    fi

    local sync_result

    sync_result="$(
        python3 - \
        "${console_conf_path}" \
        "${namespace}" \
        "${control_plane_headless_service}" \
        "${compiler_headless_service}" \
        "${control_plane_json}" \
        "${compiler_json}" <<'PY'
from pathlib import Path
import json
import re
import sys

console_conf_path = Path(sys.argv[1])
namespace = sys.argv[2]
control_plane_headless_service = sys.argv[3]
compiler_headless_service = sys.argv[4]
control_plane_json_path = Path(sys.argv[5])
compiler_json_path = Path(sys.argv[6])

control_plane_payload = json.loads(control_plane_json_path.read_text(encoding="utf-8"))
compiler_payload = json.loads(compiler_json_path.read_text(encoding="utf-8"))


def sorted_running_pods(payload):
    pods = []
    for item in payload.get("items") or []:
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        if metadata.get("deletionTimestamp"):
            continue
        if status.get("phase") != "Running":
            continue
        pods.append(item)

    def sort_key(item):
        metadata = item.get("metadata") or {}
        labels = metadata.get("labels") or {}
        index = labels.get("apps.kubernetes.io/pod-index")
        try:
            return (0, int(index))
        except (TypeError, ValueError):
            return (1, metadata.get("name") or "")

    return sorted(pods, key=sort_key)


def certname_for_pod(pod_name, headless_service_name):
    return f"{pod_name}.{headless_service_name}.{namespace}.svc.cluster.local"


def pod_name(item):
    return ((item.get("metadata") or {}).get("name") or "").strip()


control_plane_pods = sorted_running_pods(control_plane_payload)
compiler_pods = sorted_running_pods(compiler_payload)

if not control_plane_pods:
    raise SystemExit("no running control-plane pods found for console service-alert sync")

entries = []

for item in control_plane_pods:
    certname = certname_for_pod(pod_name(item), control_plane_headless_service)
    entries.extend([
        {"type": "activity", "url": f"https://{certname}:4433"},
        {"type": "classifier", "url": f"https://{certname}:4433"},
        {"type": "rbac", "url": f"https://{certname}:4433"},
        {"type": "master", "url": f"https://{certname}:8140"},
        {"type": "code-manager", "url": f"https://{certname}:8140"},
        {"type": "puppetdb", "url": f"https://{certname}:8081"},
        {"type": "orchestrator", "url": f"https://{certname}:8143"},
    ])

for item in compiler_pods:
    certname = certname_for_pod(pod_name(item), compiler_headless_service)
    entries.append({"type": "master", "url": f"https://{certname}:8140"})

service_alert_lines = ["  service-alert: ["]
for index, entry in enumerate(entries):
    suffix = "," if index < len(entries) - 1 else ""
    service_alert_lines.append(
        "    { type: \"%s\", url: \"%s\" }%s"
        % (entry["type"], entry["url"], suffix)
    )
service_alert_lines.append("  ]")
service_alert_block = "\n".join(service_alert_lines)

text = console_conf_path.read_text(encoding="utf-8")
pattern = r'^\s*service-alert:\s*\[[\s\S]*?\]\s*(?=\n\s*service-alert-timeout\s*:)'
updated, count = re.subn(pattern, service_alert_block, text, count=1, flags=re.MULTILINE)
if count != 1:
    raise SystemExit(f"unable to update service-alert block in {console_conf_path}")

if updated != text:
    console_conf_path.write_text(updated, encoding="utf-8")
    print("updated")
else:
    print("unchanged")
PY
    )"

    if [ "${sync_result}" = "updated" ]; then
        log "Updated ${console_conf_path} service-alert configuration from Kubernetes topology"
    fi

    trap - RETURN
    rm -f "${control_plane_json}" "${compiler_json}"
    return 0
}

sync_orchestration_listener_ssl_material() {
    local webserver_conf_path=/etc/puppetlabs/orchestration-services/conf.d/webserver.conf
    local orchestrator_conf_path=/etc/puppetlabs/orchestration-services/conf.d/orchestrator.conf
    local inventory_conf_path=/etc/puppetlabs/orchestration-services/conf.d/inventory.conf
    local service_cert_path=/etc/puppetlabs/orchestration-services/ssl/pe.cert.pem
    local service_key_path=/etc/puppetlabs/orchestration-services/ssl/pe.private_key.pem

    [ -f "${service_cert_path}" ] || return 0
    [ -f "${service_key_path}" ] || return 0

    python3 - "${webserver_conf_path}" "${orchestrator_conf_path}" "${inventory_conf_path}" "${service_cert_path}" "${service_key_path}" <<'PY'
from pathlib import Path
import re
import sys

cert_path = sys.argv[4]
key_path = sys.argv[5]
for config_name in sys.argv[1:4]:
    config_path = Path(config_name)
    if not config_path.is_file():
        continue
    text = config_path.read_text(encoding="utf-8")
    patterns = {
        r'^(\s*ssl-cert\s*:\s*)"[^"]+"': rf'\1"{cert_path}"',
        r'^(\s*ssl-key\s*:\s*)"[^"]+"': rf'\1"{key_path}"',
    }
    updated = text
    replaced = False
    for pattern, replacement in patterns.items():
        updated, count = re.subn(pattern, replacement, updated, flags=re.MULTILINE)
        if count < 1:
            raise SystemExit(f"unable to update listener TLS path {pattern} in {config_path}")
        replaced = replaced or bool(count)
    if replaced and updated != text:
        config_path.write_text(updated, encoding="utf-8")
PY

    log "Configured orchestration listener TLS material through ${webserver_conf_path}, ${orchestrator_conf_path}, and ${inventory_conf_path}"
}

patch_conductor_console_auth_barrier_ports() {
    local enabled="${PE_K8S_CONDUCTOR_RBAC_SYNC_ENABLED:-false}"
    local webserver_conf_path=/etc/puppetlabs/console-services/conf.d/webserver.conf

    [ "${enabled}" = "true" ] || return 0
    [ -f "${webserver_conf_path}" ] || return 0

    python3 - "${webserver_conf_path}" <<'PY'
from pathlib import Path
import re
import sys

webserver_conf_path = Path(sys.argv[1])
text = webserver_conf_path.read_text(encoding="utf-8")
if 'port: "4440"' in text and 'ssl-port: 4433' in text:
    raise SystemExit(0)

updated, console_count = re.subn(
    r'(\n\s*port:\s*")4430(")',
    r'\g<1>4440\2',
    text,
    count=1,
)
updated, api_count = re.subn(
    r'(\n\s*ssl-port:\s*)4443\b',
    r'\g<1>4433',
    updated,
    count=1,
)
if console_count != 1 and 'port: "4440"' not in updated:
    raise SystemExit(f"unable to patch auth barrier ports in {webserver_conf_path}")
if api_count != 1 and 'ssl-port: 4433' not in updated:
    raise SystemExit(f"unable to patch auth barrier ports in {webserver_conf_path}")
if updated != text:
    webserver_conf_path.write_text(updated, encoding="utf-8")
PY

    log "Patched console-services listener ports for the Conductor auth barrier"
}
sync_conductor_console_auth_shared_state() {
    local enabled="${PE_K8S_CONDUCTOR_RBAC_SYNC_ENABLED:-false}"
    local rbac_conf_path=/etc/puppetlabs/console-services/conf.d/rbac.conf
    local shared_secret_dir=/etc/puppetlabs/console-services/conf.d/secrets/conductor

    [ "${enabled}" = "true" ] || return 0

    if [ ! -f "${rbac_conf_path}" ]; then
        log "RBAC config missing; skipping Conductor console auth shared-state sync"
        return 0
    fi

    python3 - "${rbac_conf_path}" "${shared_secret_dir}" <<'PY'
from pathlib import Path
import json
import re
import shutil
import sys

rbac_conf_path = Path(sys.argv[1])
shared_secret_dir = Path(sys.argv[2])
text = rbac_conf_path.read_text(encoding="utf-8")

patterns = {
    "tokenPrivateKey": r'^\s*token-private-key\s*:\s*"([^"]+)"',
    "tokenPublicKey": r'^\s*token-public-key\s*:\s*"([^"]+)"',
    "samlKey": r'^\s*saml-key\s*:\s*"([^"]+)"',
    "samlCert": r'^\s*saml-cert\s*:\s*"([^"]+)"',
}

current = {}
for name, pattern in patterns.items():
    match = re.search(pattern, text, re.MULTILINE)
    current[name] = Path(match.group(1)) if match else None

target = {
    "tokenPrivateKey": shared_secret_dir / "token-signing.private_key.pem",
    "tokenPublicKey": shared_secret_dir / "token-signing.cert.pem",
    "samlKey": shared_secret_dir / "saml.private_key.pem",
    "samlCert": shared_secret_dir / "saml.cert.pem",
}

shared_secret_dir.mkdir(parents=True, exist_ok=True)

for name in ("tokenPrivateKey", "tokenPublicKey"):
    source = current.get(name)
    if source is None or not source.is_file():
        raise SystemExit(f"required RBAC auth file is missing: {source or name}")
    destination = target[name]
    if not destination.exists():
        shutil.copyfile(source, destination)
        shutil.copystat(source, destination, follow_symlinks=True)

for name in ("samlKey", "samlCert"):
    source = current.get(name)
    if source is None or not source.is_file():
        continue
    destination = target[name]
    if not destination.exists():
        shutil.copyfile(source, destination)
        shutil.copystat(source, destination, follow_symlinks=True)

replacements = {
    "token-private-key": str(target["tokenPrivateKey"]),
    "token-public-key": str(target["tokenPublicKey"]),
}
if current.get("samlKey") and current["samlKey"].is_file():
    replacements["saml-key"] = str(target["samlKey"])
if current.get("samlCert") and current["samlCert"].is_file():
    replacements["saml-cert"] = str(target["samlCert"])

updated = text
for key, value in replacements.items():
    pattern = rf'^(\s*{re.escape(key)}\s*:\s*)"([^"]+)"'
    updated, count = re.subn(pattern, '\\1' + json.dumps(value), updated, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"unable to update {key} in {rbac_conf_path}")

if updated != text:
    rbac_conf_path.write_text(updated, encoding="utf-8")
PY

    log "Configured shared RBAC auth material through ${rbac_conf_path}"
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

    ensure_pe_puppetserver_sysconfig
}

ensure_pe_puppetserver_sysconfig() {
    local path=/etc/sysconfig/pe-puppetserver

    [ -f "${path}" ] && return 0

    ensure_dir /etc/sysconfig
    cat > "${path}" <<'EOF'
###########################################
# Init settings for pe-puppetserver
###########################################

JAVA_BIN="/opt/puppetlabs/server/bin/java"
JAVA_ARGS="-Xmx2048m -Xms2048m -Xss2m -Djava.io.tmpdir=/opt/puppetlabs/server/apps/puppetserver/tmp -XX:ReservedCodeCacheSize=512m -Xlog:gc*:file=/var/log/puppetlabs/puppetserver/puppetserver_gc.log:time,uptime,level,tags:filecount=16,filesize=16m -Djdk.tls.ephemeralDHKeySize=2048 -XX:+UseStringDeduplication -Djava.security.properties==/opt/puppetlabs/share/jdk17-security"
JAVA_ARGS_CLI="${JAVA_ARGS_CLI:-}"
TK_ARGS=""
USER=pe-puppet
GROUP=pe-puppet
INSTALL_DIR="/opt/puppetlabs/server/apps/puppetserver"
CONFIG="/etc/puppetlabs/puppetserver/conf.d"
BOOTSTRAP_CONFIG="/etc/puppetlabs/puppetserver/bootstrap.cfg"
SERVICE_STOP_RETRIES=60
START_TIMEOUT=300
OPEN_FILE_LIMIT=12000
RELOAD_TIMEOUT=300
JRUBY_JAR="/opt/puppetlabs/server/apps/puppetserver/jruby-9k.jar"
BC_JAR="/opt/puppetlabs/share/java/bcprov.jar:/opt/puppetlabs/share/java/bcpkix.jar:/opt/puppetlabs/share/java/bcutil.jar:/opt/puppetlabs/share/java/bctls.jar"
EOF
    log "Seeded fallback /etc/sysconfig/pe-puppetserver"
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

ensure_postgresql_server_bin_alternatives() {
    local alternatives_dir=/etc/alternatives
    local source_dir=/opt/puppetlabs/server/apps/postgresql/14/bin
    local name source target

    ensure_dir "${alternatives_dir}"

    for name in \
        psql \
        createdb \
        initdb \
        pg_ctl \
        pg_isready \
        pg_dump \
        pg_restore
    do
        source="${source_dir}/${name}"

        case "${name}" in
            psql)
                target="${alternatives_dir}/pe-postgresql"
                ;;
            createdb)
                target="${alternatives_dir}/pe-postgresql-createdb"
                ;;
            initdb)
                target="${alternatives_dir}/pe-postgresql-server-initdb"
                ;;
            pg_ctl)
                target="${alternatives_dir}/pe-postgresql-server-pg_ctl"
                ;;
            pg_isready)
                target="${alternatives_dir}/pe-postgresql-pg_isready"
                ;;
            pg_dump)
                target="${alternatives_dir}/pe-postgresql-pg_dump"
                ;;
            pg_restore)
                target="${alternatives_dir}/pe-postgresql-pg_restore"
                ;;
        esac

        ln -sfn "${source}" "${target}"
    done
}

postgresql_data_dir() {
    printf '%s\n' "${PGDATA:-/opt/puppetlabs/server/data/postgresql/14/data}"
}

postgresql_port() {
    printf '%s\n' "${PGPORT:-5432}"
}

postgresql_run_dir() {
    printf '%s\n' "${PGSOCKETDIR:-/var/run/puppetlabs/postgresql14}"
}

clear_stale_postgresql_state() {
    local data_dir="${1:-$(postgresql_data_dir)}"
    local port="${2:-$(postgresql_port)}"
    local run_dir="${3:-$(postgresql_run_dir)}"
    local pid_file="${data_dir}/postmaster.pid"
    local pid=""
    local status=""

    [ -f "${pid_file}" ] || return 0

    if /opt/puppetlabs/server/apps/postgresql/14/bin/pg_isready -h 127.0.0.1 -p "${port}" >/dev/null 2>&1; then
        return 0
    fi

    pid="$(sed -n '1p' "${pid_file}" 2>/dev/null | tr -d '[:space:]')"
    status="$(sed -n '8p' "${pid_file}" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
        if [ "${status}" = "stopping" ]; then
            log "Observed PostgreSQL pid ${pid} stuck in stopping state; clearing stale runtime state"
            kill -TERM "${pid}" 2>/dev/null || true
            sleep 2
        else
            return 0
        fi
    fi

    if /opt/puppetlabs/server/apps/postgresql/14/bin/pg_isready -h 127.0.0.1 -p "${port}" >/dev/null 2>&1; then
        return 0
    fi

    rm -f \
        "${pid_file}" \
        "${run_dir}/.s.PGSQL.${port}" \
        "${run_dir}/.s.PGSQL.${port}.lock"
    log "Removed stale PostgreSQL state from ${data_dir}"
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
    local certname="${3:-}"
    local pk8_path
    local dir
    local backup_base
    local backup_dir
    local cert_variant_path
    local key_variant_path
    local pk8_variant_path

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

        if [ -n "${certname}" ]; then
            cert_variant_path="${dir}/${certname}.cert.pem"
            key_variant_path="${dir}/${certname}.private_key.pem"
            pk8_variant_path="${dir}/${certname}.private_key.pk8"

            cp -f "${cert_path}" "${cert_variant_path}"
            cp -f "${key_path}" "${key_variant_path}"
            cp -f "${pk8_path}" "${pk8_variant_path}"

            if [ -f "${cert_variant_path}" ]; then
                chown --reference="${dir}/pe.cert.pem" "${cert_variant_path}"
                chmod --reference="${dir}/pe.cert.pem" "${cert_variant_path}"
            fi

            if [ -f "${key_variant_path}" ]; then
                chown --reference="${dir}/pe.private_key.pem" "${key_variant_path}"
                chmod --reference="${dir}/pe.private_key.pem" "${key_variant_path}"
            fi

            if [ -f "${pk8_variant_path}" ]; then
                chown --reference="${dir}/pe.private_key.pk8" "${pk8_variant_path}"
                chmod --reference="${dir}/pe.private_key.pk8" "${pk8_variant_path}"
            fi
        fi
    done < <(pe_service_ssl_targets)

    rm -f "${pk8_path}"
}

repair_pe_service_ssl_material() {
    local certname="$1"

    sync_pe_service_ssl_material \
        "/etc/puppetlabs/puppet/ssl/certs/${certname}.pem" \
        "/etc/puppetlabs/puppet/ssl/private_keys/${certname}.pem" \
        "${certname}"
}

patch_nginx_ingress_redirects() {
    local path

    python3 - <<'PY'
from pathlib import Path

location_block = """location = /rbac-api/v1/auth/token
{
proxy_pass https://127.0.0.1:4444;
proxy_redirect https://127.0.0.1:4444 /;
proxy_read_timeout 120;
proxy_set_header X-SSL-Subject $ssl_client_s_dn;
proxy_set_header X-Client-DN $ssl_client_s_dn;
proxy_set_header X-Client-Verify $ssl_client_verify;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Proto https;
}
"""

proxy_conf_path = Path("/etc/puppetlabs/nginx/conf.d/proxy.conf")
if proxy_conf_path.is_file():
    text = proxy_conf_path.read_text(encoding="utf-8")
    if "location = /rbac-api/v1/auth/token" not in text:
        marker = "location /\n{"
        if marker not in text:
            raise SystemExit(f"unable to locate nginx location block in {proxy_conf_path}")
        text = text.replace(marker, f"{location_block}\n{marker}", 1)
        proxy_conf_path.write_text(text, encoding="utf-8")
PY

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

  location = /rbac-api/v1/auth/token {
    proxy_pass https://127.0.0.1:4444;
    proxy_redirect https://127.0.0.1:4444 /;
    proxy_read_timeout 120;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
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
