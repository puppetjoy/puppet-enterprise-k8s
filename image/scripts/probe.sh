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
    local local_status pe_status puppetdb_status

    [ -n "${PE_K8S_COMPILER_PE_SERVICE:-}" ] || return 1

    /opt/puppetlabs/server/apps/postgresql/14/bin/pg_isready \
        -h 127.0.0.1 \
        -p "${PGPORT:-5432}" >/dev/null 2>&1
    puppetdb_status="$(curl -skf https://127.0.0.1:8081/status/v1/services?level=debug)"

    local_status="$(curl -skf https://127.0.0.1:8140/status/v1/services?level=debug)"
    pe_status="$(curl -skf "https://${PE_K8S_COMPILER_PE_SERVICE}:8140/status/v1/services?level=debug")"

    python3 - "${local_status}" "${pe_status}" "${puppetdb_status}" "${PE_K8S_COMPILER_PUPPETDB_SYNC_MAX_AGE_SECONDS:-}" <<'PY'
import json
import os
import re
import sys
from datetime import datetime, timezone

local = json.loads(sys.argv[1])
pe = json.loads(sys.argv[2])
puppetdb = json.loads(sys.argv[3])
configured_max_age = sys.argv[4].strip()

def sync_max_age_seconds():
    if configured_max_age:
        return int(configured_max_age)

    interval = ""
    path = "/etc/puppetlabs/puppetdb/conf.d/sync.ini"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                match = re.match(r"^\s*intervals\s*=\s*(\S+)\s*$", line)
                if match:
                    interval = match.group(1)
                    break

    match = re.fullmatch(r"(\d+)([smhd])", interval)
    if not match:
        return 900

    value = int(match.group(1))
    unit = match.group(2)
    seconds_by_unit = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }
    return value * seconds_by_unit[unit] * 3

puppetdb_status = (puppetdb.get("puppetdb-status") or {}).get("status") or {}
if not puppetdb_status.get("read_db_up?"):
    raise SystemExit(1)
if not puppetdb_status.get("write_db_up?"):
    raise SystemExit(1)

sync_status = puppetdb_status.get("sync_status") or {}
if sync_status.get("state") not in {"idle", "syncing"}:
    raise SystemExit(1)

last_successful_sync = sync_status.get("last_successful_sync") or ""
if not last_successful_sync:
    raise SystemExit(1)

last_sync_time = datetime.fromisoformat(last_successful_sync.replace("Z", "+00:00"))
sync_age_seconds = (datetime.now(timezone.utc) - last_sync_time).total_seconds()
if sync_age_seconds > sync_max_age_seconds():
    raise SystemExit(1)

local_fs = local.get("file-sync-client-service", {})
if local_fs.get("state") != "running":
    raise SystemExit(1)

local_repo = (((local_fs.get("status") or {}).get("repos") or {}).get("puppet-code") or {})
if local_repo.get("status") != "ok":
    raise SystemExit(1)

pe_fs = pe.get("file-sync-storage-service", {})
if pe_fs.get("state") != "running":
    raise SystemExit(1)

pe_repo = (((pe_fs.get("status") or {}).get("repos") or {}).get("puppet-code") or {})

def latest_commit(repo):
    return (((repo.get("latest_commit") or {}).get("commit")) or "")

if latest_commit(local_repo) != latest_commit(pe_repo):
    raise SystemExit(1)

pe_submodules = pe_repo.get("submodules") or {}
local_submodules = local_repo.get("submodules") or {}
for name, repo in pe_submodules.items():
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
