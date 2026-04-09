#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

role="${1:-${PE_K8S_ROLE:-}}"
[ -n "${role}" ] || exit 1

wait_for_install_marker

curl_with_local_role_cert() {
    local cert_dir="$1"
    shift

    exec curl -skf \
        --cert "${cert_dir}/pe.cert.pem" \
        --key "${cert_dir}/pe.private_key.pem" \
        --cacert /etc/puppetlabs/puppet/ssl/certs/ca.pem \
        "$@"
}

compiler_filesync_ready() {
    local local_status primary_status

    [ -n "${PE_K8S_COMPILER_PRIMARY_SERVICE:-}" ] || return 1

    local_status="$(curl -skf https://127.0.0.1:8140/status/v1/services?level=debug)"
    primary_status="$(curl -skf "https://${PE_K8S_COMPILER_PRIMARY_SERVICE}:8140/status/v1/services?level=debug")"

    python3 - "${local_status}" "${primary_status}" <<'PY'
import json
import sys

local = json.loads(sys.argv[1])
primary = json.loads(sys.argv[2])

local_fs = local.get("file-sync-client-service", {})
if local_fs.get("state") != "running":
    raise SystemExit(1)

local_repo = (((local_fs.get("status") or {}).get("repos") or {}).get("puppet-code") or {})
if local_repo.get("status") != "ok":
    raise SystemExit(1)

primary_fs = primary.get("file-sync-storage-service", {})
if primary_fs.get("state") != "running":
    raise SystemExit(1)

primary_repo = (((primary_fs.get("status") or {}).get("repos") or {}).get("puppet-code") or {})

def latest_commit(repo):
    return (((repo.get("latest_commit") or {}).get("commit")) or "")

if latest_commit(local_repo) != latest_commit(primary_repo):
    raise SystemExit(1)

primary_submodules = primary_repo.get("submodules") or {}
local_submodules = local_repo.get("submodules") or {}
for name, repo in primary_submodules.items():
    local_submodule = local_submodules.get(name) or {}
    if local_submodule.get("status") != "ok":
        raise SystemExit(1)
    if latest_commit(local_submodule) != latest_commit(repo):
        raise SystemExit(1)
PY
}

case "${role}" in
    postgresql)
        exec /opt/puppetlabs/server/apps/postgresql/14/bin/pg_isready \
            -h 127.0.0.1 \
            -p "${PGPORT:-5432}"
        ;;
    puppetdb)
        exec curl -skf https://127.0.0.1:8081/status/v1/services/status-service
        ;;
    puppetserver)
        exec curl -skf https://127.0.0.1:8140/status/v1/services
        ;;
    compiler-puppetserver)
        compiler_filesync_ready
        ;;
    nginx)
        exec curl -skf https://127.0.0.1:443/
        ;;
    console-services)
        exec curl -skf https://127.0.0.1:4433/status/v1/services/status-service
        ;;
    orchestration-services)
        exec curl -skf https://127.0.0.1:8143/status/v1/services/status-service
        ;;
    host-action-collector)
        exec curl -skf https://127.0.0.1:8147/status/v1/services
        ;;
    bolt-server)
        curl_with_local_role_cert /etc/puppetlabs/bolt-server/ssl \
            https://127.0.0.1:62658/admin/status
        ;;
    ace-server)
        curl_with_local_role_cert /etc/puppetlabs/ace-server/ssl \
            https://127.0.0.1:44633/admin/status
        ;;
    *)
        exit 1
        ;;
esac
