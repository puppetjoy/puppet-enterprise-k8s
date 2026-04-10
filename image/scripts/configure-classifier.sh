#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_CLASSIFIER_PE_SERVICE="${PE_CLASSIFIER_PE_SERVICE:-pe}"
PE_CLASSIFIER_PE_CONF_PATH="${PE_CLASSIFIER_PE_CONF_PATH:-/config/pe.conf}"
PE_CLASSIFIER_WAIT_TIMEOUT_SECONDS="${PE_CLASSIFIER_WAIT_TIMEOUT_SECONDS:-900}"
PE_CLASSIFIER_LOGIN="${PE_CLASSIFIER_LOGIN:-admin}"
PE_CLASSIFIER_PASSWORD="${PE_CLASSIFIER_PASSWORD:-}"
PE_CLASSIFIER_AGENT_GROUP_NAME="${PE_CLASSIFIER_AGENT_GROUP_NAME:-PE Agent}"
PE_CLASSIFIER_MASTER_GROUP_NAME="${PE_CLASSIFIER_MASTER_GROUP_NAME:-PE Master}"
PE_CLASSIFIER_COMPILER_HOST="${PE_CLASSIFIER_COMPILER_HOST:-pe-compiler}"
PE_CLASSIFIER_SERVER_LIST="${PE_CLASSIFIER_SERVER_LIST:-${PE_CLASSIFIER_COMPILER_HOST}:8140}"
PE_CLASSIFIER_PRIMARY_URIS="${PE_CLASSIFIER_PRIMARY_URIS:-https://${PE_CLASSIFIER_COMPILER_HOST}:8140}"
PE_CLASSIFIER_PCP_BROKER_LIST="${PE_CLASSIFIER_PCP_BROKER_LIST:-${PE_CLASSIFIER_COMPILER_HOST}:8142}"
PE_CLASSIFIER_COMPILE_MASTER_POOL_ADDRESS="${PE_CLASSIFIER_COMPILE_MASTER_POOL_ADDRESS:-${PE_CLASSIFIER_COMPILER_HOST}}"

classifier_password() {
    if [ -n "${PE_CLASSIFIER_PASSWORD}" ]; then
        printf '%s\n' "${PE_CLASSIFIER_PASSWORD}"
        return 0
    fi

    hocon_string_setting console_admin_password "${PE_CLASSIFIER_PE_CONF_PATH}"
}

classifier_api_get() {
    local token="$1"
    local path="$2"

    curl -sk \
        -H "X-Authentication: ${token}" \
        "https://${PE_CLASSIFIER_PE_SERVICE}:4433${path}"
}

classifier_api_post() {
    local token="$1"
    local path="$2"
    local payload_path="$3"

    curl -sk \
        -H "X-Authentication: ${token}" \
        -H "Content-Type: application/json" \
        --request POST \
        "https://${PE_CLASSIFIER_PE_SERVICE}:4433${path}" \
        --data "@${payload_path}"
}

group_id_by_name() {
    local token="$1"
    local group_name="$2"
    local groups_path

    groups_path="$(mktemp)"
    classifier_api_get "${token}" "/classifier-api/v1/groups" > "${groups_path}"
    python3 - "${groups_path}" "${group_name}" <<'PY'
import json
import sys

groups_path, group_name = sys.argv[1], sys.argv[2]
with open(groups_path, "r", encoding="utf-8") as fh:
    groups = json.load(fh)

for group in groups:
    if group.get("name") == group_name:
        print(group["id"])
        raise SystemExit(0)

raise SystemExit(1)
PY
    rm -f "${groups_path}"
}

array_json_from_csv() {
    python3 - "$1" <<'PY'
import json
import sys

items = []
for entry in sys.argv[1].split(","):
    entry = entry.strip()
    if entry and entry not in items:
        items.append(entry)

print(json.dumps(items, separators=(",", ":")))
PY
}

agent_group_params_json() {
    local server_list_json
    local primary_uris_json
    local pcp_broker_list_json

    server_list_json="$(array_json_from_csv "${PE_CLASSIFIER_SERVER_LIST}")"
    primary_uris_json="$(array_json_from_csv "${PE_CLASSIFIER_PRIMARY_URIS}")"
    pcp_broker_list_json="$(array_json_from_csv "${PE_CLASSIFIER_PCP_BROKER_LIST}")"

    python3 - "${server_list_json}" "${primary_uris_json}" "${pcp_broker_list_json}" <<'PY'
import json
import sys

print(json.dumps({
    "manage_puppet_conf": True,
    "server_list": json.loads(sys.argv[1]),
    "primary_uris": json.loads(sys.argv[2]),
    "pcp_broker_list": json.loads(sys.argv[3]),
}, separators=(",", ":")))
PY
}

pe_repo_params_json() {
    python3 - "${PE_CLASSIFIER_COMPILE_MASTER_POOL_ADDRESS}" <<'PY'
import json
import sys

print(json.dumps({
    "compile_master_pool_address": sys.argv[1],
}, separators=(",", ":")))
PY
}

ensure_group_class_params() {
    local token="$1"
    local group_name="$2"
    local class_name="$3"
    local desired_json="$4"
    local group_id
    local group_path
    local delta_path

    group_id="$(group_id_by_name "${token}" "${group_name}")" || {
        log "Unable to find classifier group ${group_name}"
        return 1
    }

    group_path="$(mktemp)"
    delta_path="$(mktemp)"
    classifier_api_get "${token}" "/classifier-api/v1/groups/${group_id}" > "${group_path}"

    python3 - "${group_path}" "${class_name}" "${desired_json}" "${delta_path}" <<'PY'
import json
import sys

group_path, class_name, desired_json, delta_path = sys.argv[1:5]
with open(group_path, "r", encoding="utf-8") as fh:
    group = json.load(fh)

desired = json.loads(desired_json)
classes = group.get("classes") or {}
current = classes.get(class_name) or {}
merged = dict(current)
merged.update(desired)

if current == merged:
    raise SystemExit(0)

with open(delta_path, "w", encoding="utf-8") as fh:
    json.dump({"classes": {class_name: merged}}, fh, separators=(",", ":"))
PY

    if [ -s "${delta_path}" ]; then
        classifier_api_post "${token}" "/classifier-api/v1/groups/${group_id}" "${delta_path}" >/dev/null
        log "Updated classifier group ${group_name} class ${class_name}"
    else
        log "Classifier group ${group_name} class ${class_name} already matches"
    fi

    rm -f "${group_path}" "${delta_path}"
}

main() {
    local password
    local token
    local agent_params
    local pe_repo_params

    password="$(classifier_password || true)"
    [ -n "${password}" ] || {
        log "Unable to determine console admin password for classifier configuration"
        exit 1
    }

    wait_for_remote_pe_status "${PE_CLASSIFIER_PE_SERVICE}" "${PE_CLASSIFIER_WAIT_TIMEOUT_SECONDS}"
    wait_for_remote_console "${PE_CLASSIFIER_PE_SERVICE}" "${PE_CLASSIFIER_WAIT_TIMEOUT_SECONDS}"
    wait_for_remote_pe_status "${PE_CLASSIFIER_COMPILER_HOST}" "${PE_CLASSIFIER_WAIT_TIMEOUT_SECONDS}"

    token="$(issue_rbac_token "${PE_CLASSIFIER_PE_SERVICE}" "${PE_CLASSIFIER_LOGIN}" "${password}" "1h" "pe-k8s-classifier")" || {
        log "Failed to generate an RBAC token for classifier configuration"
        exit 1
    }

    agent_params="$(agent_group_params_json)"
    pe_repo_params="$(pe_repo_params_json)"

    ensure_group_class_params "${token}" "${PE_CLASSIFIER_AGENT_GROUP_NAME}" "puppet_enterprise::profile::agent" "${agent_params}"
    ensure_group_class_params "${token}" "${PE_CLASSIFIER_MASTER_GROUP_NAME}" "pe_repo" "${pe_repo_params}"
}

main "$@"
