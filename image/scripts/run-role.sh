#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

ensure_postgresql_server_bin_alternatives

role="${1:-${PE_K8S_ROLE:-}}"
[ -n "${role}" ] || {
    log "PE role is required"
    exit 1
}

role_user() {
    case "${1}" in
        postgresql) printf '%s\n' pe-postgres ;;
        puppetdb) printf '%s\n' pe-puppetdb ;;
        puppetserver) printf '%s\n' pe-puppet ;;
        nginx) printf '%s\n' pe-webserver ;;
        console-services) printf '%s\n' pe-console-services ;;
        orchestration-services) printf '%s\n' pe-orchestration-services ;;
        host-action-collector) printf '%s\n' pe-host-action-collector ;;
        bolt-server) printf '%s\n' pe-bolt-server ;;
        ace-server) printf '%s\n' pe-ace-server ;;
        *) return 1 ;;
    esac
}

role_group() {
    role_user "$1"
}

role_runtime_dirs() {
    case "${1}" in
        postgresql)
            printf '%s\n' /var/run/puppetlabs/postgresql
            printf '%s\n' /var/run/puppetlabs/postgresql14
            printf '%s\n' /var/log/puppetlabs/postgresql
            printf '%s\n' /var/log/puppetlabs/postgresql/14
            ;;
        puppetdb)
            printf '%s\n' /var/run/puppetlabs/puppetdb
            printf '%s\n' /var/log/puppetlabs/puppetdb
            ;;
        puppetserver)
            printf '%s\n' /var/run/puppetlabs/puppetserver
            printf '%s\n' /var/log/puppetlabs/puppetserver
            ;;
        nginx)
            printf '%s\n' /var/run/puppetlabs/nginx
            printf '%s\n' /var/log/puppetlabs/nginx
            printf '%s\n' /var/cache/puppetlabs/nginx
            ;;
        console-services)
            printf '%s\n' /var/run/puppetlabs/console-services
            printf '%s\n' /var/log/puppetlabs/console-services
            ;;
        orchestration-services)
            printf '%s\n' /var/run/puppetlabs/orchestration-services
            printf '%s\n' /var/log/puppetlabs/orchestration-services
            ;;
        host-action-collector)
            printf '%s\n' /var/run/puppetlabs/host-action-collector
            printf '%s\n' /var/log/puppetlabs/host-action-collector
            ;;
        bolt-server)
            printf '%s\n' /var/run/puppetlabs/bolt-server
            printf '%s\n' /var/log/puppetlabs/bolt-server
            ;;
        ace-server)
            printf '%s\n' /var/run/puppetlabs/ace-server
            printf '%s\n' /var/log/puppetlabs/ace-server
            ;;
    esac
}

ensure_role_runtime_dirs() {
    local user group dir

    user="$(role_user "${role}" 2>/dev/null || true)"
    group="$(role_group "${role}" 2>/dev/null || true)"
    ensure_dir /var/run/puppetlabs
    chmod 0755 /var/run/puppetlabs || true

    while IFS= read -r dir; do
        [ -n "${dir}" ] || continue
        ensure_dir "${dir}"
        if [ -n "${user}" ] && [ -n "${group}" ]; then
            chown "${user}:${group}" "${dir}"
        fi
        chmod 0750 "${dir}" || true
    done < <(role_runtime_dirs "${role}")
}

wait_for_install_marker
copy_exported_sysconfig_into_rootfs
if [ "${role}" = "puppetserver" ]; then
    sync_puppetdb_integration_settings
    sync_relay_puppetdb_command_submission_settings
    sync_relay_code_manager_post_environment_hooks
    sync_compiler_file_sync_service_urls
fi
ensure_role_runtime_dirs

wait_for_postgresql() {
    local host="${PE_K8S_PUPPETDB_DATABASE_HOST:-}"
    local port="${PE_K8S_PUPPETDB_DATABASE_PORT:-5432}"
    local attempts="${PE_K8S_PUPPETDB_DATABASE_WAIT_ATTEMPTS:-120}"
    local interval="${PE_K8S_PUPPETDB_DATABASE_WAIT_INTERVAL_SECONDS:-2}"
    local i

    [ -n "${host}" ] || return 0

    for i in $(seq 1 "${attempts}"); do
        if /opt/puppetlabs/server/apps/postgresql/14/bin/pg_isready -h "${host}" -p "${port}" >/dev/null 2>&1; then
            return 0
        fi
        sleep "${interval}"
    done

    log "Timed out waiting for PostgreSQL at ${host}:${port}"
    return 1
}

CONTROL_PLANE_CA_BUNDLE_SYNC_PID=""
PUPPETSERVER_CHILD_PID=""

stop_control_plane_ca_bundle_sync_loop() {
    [ -n "${CONTROL_PLANE_CA_BUNDLE_SYNC_PID}" ] || return 0
    if kill -0 "${CONTROL_PLANE_CA_BUNDLE_SYNC_PID}" 2>/dev/null; then
        kill "${CONTROL_PLANE_CA_BUNDLE_SYNC_PID}" 2>/dev/null || true
    fi
    CONTROL_PLANE_CA_BUNDLE_SYNC_PID=""
}

control_plane_ca_bundle_target_matches() {
    local path="$1"
    local source_path="$2"
    local mode="$3"
    local owner_group="$4"
    local actual=""

    [ -f "${path}" ] || return 1
    [ "$(sha256sum "${source_path}" | awk '{print $1}')" = "$(sha256sum "${path}" | awk '{print $1}')" ] || return 1

    actual="$(stat -c '%U:%G %a' "${path}" 2>/dev/null || true)"
    [ "${actual}" = "${owner_group} ${mode}" ]
}

merge_control_plane_crl_bundle() {
    local output_path="$1"
    shift

    python3 - "$output_path" "$@" <<'PY'
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def datetime_sort_value(value):
    value = normalize_datetime(value)
    return int(value.timestamp()) if value is not None else 0


def split_pem_blocks(content, label):
    pattern = re.compile(
        rf"-----BEGIN {re.escape(label)}-----\s*.*?-----END {re.escape(label)}-----\s*",
        re.DOTALL,
    )
    blocks = []
    for match in pattern.finditer(content or ""):
        block = match.group(0).strip()
        if block:
            blocks.append(block + "\n")
    return blocks


selected = {}
for raw_path in sys.argv[2:]:
    if not raw_path:
        continue
    path = Path(raw_path)
    if not path.is_file():
        continue
    content = path.read_text(encoding="utf-8")
    for block in split_pem_blocks(content, "X509 CRL"):
        crl = x509.load_pem_x509_crl(block.encode("utf-8"))
        issuer = crl.issuer.rfc4514_string()
        try:
            crl_number = crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
        except x509.ExtensionNotFound:
            crl_number = 0
        sort_key = (
            crl_number,
            datetime_sort_value(getattr(crl, "next_update", None)),
            datetime_sort_value(getattr(crl, "last_update", None)),
            sha256_text(block),
        )
        current = selected.get(issuer)
        if current is None or sort_key > current[0]:
            selected[issuer] = (sort_key, block)

Path(sys.argv[1]).write_text(
    "".join(selected[issuer][1] for issuer in sorted(selected)),
    encoding="utf-8",
)
PY
}

sync_control_plane_ca_bundle_from_dir() {
    local bundle_dir="$1"

    ensure_dir /etc/puppetlabs/puppet/ssl/certs
    ensure_dir /etc/puppetlabs/puppetserver/ca
    install -o pe-puppet -g pe-puppet -m 0644 \
        "${bundle_dir}/ca.pem" \
        /etc/puppetlabs/puppet/ssl/certs/ca.pem
    install -o pe-puppet -g pe-puppet -m 0640 \
        "${bundle_dir}/ca.pem" \
        /etc/puppetlabs/puppetserver/ca/ca_crt.pem
    install -o pe-puppet -g pe-puppet -m 0644 \
        "${bundle_dir}/crl.pem" \
        /etc/puppetlabs/puppet/ssl/crl.pem
    install -o pe-puppet -g pe-puppet -m 0644 \
        "${bundle_dir}/crl.pem" \
        "$(control_plane_hostcrl_path)"
    install -o pe-puppet -g pe-puppet -m 0640 \
        "${bundle_dir}/crl.pem" \
        /etc/puppetlabs/puppetserver/ca/ca_crl.pem
    install -o pe-puppet -g pe-puppet -m 0640 \
        "${bundle_dir}/crl.pem" \
        /etc/puppetlabs/puppetserver/ca/infra_crl.pem
}

sync_control_plane_trust_bundle_runtime_from_dir() {
    local bundle_dir="$1"
    local crl_path="${2:-${bundle_dir}/crl.pem}"

    ensure_dir /etc/puppetlabs/puppet/ssl/certs
    install -o pe-puppet -g pe-puppet -m 0644 \
        "${bundle_dir}/ca.pem" \
        /etc/puppetlabs/puppet/ssl/certs/ca.pem
    install -o pe-puppet -g pe-puppet -m 0640 \
        "${bundle_dir}/ca.pem" \
        /etc/puppetlabs/puppetserver/ca/ca_crt.pem
    install -o pe-puppet -g pe-puppet -m 0644 \
        "${crl_path}" \
        /etc/puppetlabs/puppet/ssl/crl.pem
    install -o pe-puppet -g pe-puppet -m 0644 \
        "${crl_path}" \
        "$(control_plane_hostcrl_path)"
}

sync_control_plane_ca_bundle_once() {
    local bundle_secret_name="${PE_CONTROL_PLANE_CA_BUNDLE_SECRET_NAME:-}"
    local runtime_bundle_secret_name="${PE_CONTROL_PLANE_RUNTIME_TRUST_BUNDLE_SECRET_NAME:-}"

    [ -n "${bundle_secret_name}" ] || [ -n "${runtime_bundle_secret_name}" ] || return 0

    (
        set -euo pipefail

        local namespace bundle_dir source_secret_name
        namespace="$(k8s_namespace)"
        source_secret_name="$(
            select_first_present_secret \
                "${namespace}" \
                "${runtime_bundle_secret_name}" \
                "${bundle_secret_name}" \
                || true
        )"
        [ -n "${source_secret_name}" ] || return 0
        bundle_dir="$(mktemp -d /tmp/pe-k8s-control-plane-ca-once.XXXXXX)"
        trap 'rm -rf "${bundle_dir}"' EXIT

        k8s_secret_data_field "${namespace}" "${source_secret_name}" ca.pem > "${bundle_dir}/ca.pem"
        k8s_secret_data_field "${namespace}" "${source_secret_name}" crl.pem > "${bundle_dir}/crl.pem"
        sync_control_plane_ca_bundle_from_dir "${bundle_dir}"
    )
}

sync_control_plane_hostcrl_setting() {
    local puppet_bin=/opt/puppetlabs/bin/puppet
    local puppet_conf=/etc/puppetlabs/puppet/puppet.conf

    [ -x "${puppet_bin}" ] || return 0
    [ -f "${puppet_conf}" ] || return 0

    "${puppet_bin}" config set hostcrl "$(control_plane_hostcrl_path)" --section server >/dev/null
}

start_control_plane_ca_bundle_sync_loop() {
    local bundle_secret_name="${PE_CONTROL_PLANE_CA_BUNDLE_SECRET_NAME:-}"
    local runtime_bundle_secret_name="${PE_CONTROL_PLANE_RUNTIME_TRUST_BUNDLE_SECRET_NAME:-}"
    local poll_seconds="${PE_CONTROL_PLANE_CA_BUNDLE_POLL_SECONDS:-5}"

    [ -n "${bundle_secret_name}" ] || [ -n "${runtime_bundle_secret_name}" ] || return 0

    (
        set -euo pipefail

        local namespace bundle_dir fingerprint=""
        local current_fingerprint=""
        local runtime_crl_path
        local active_secret_name=""
        local source_secret_name=""

        namespace="$(k8s_namespace)"
        bundle_dir="$(mktemp -d /tmp/pe-k8s-control-plane-ca.XXXXXX)"
        runtime_crl_path="${bundle_dir}/runtime-crl.pem"
        trap 'rm -rf "${bundle_dir}"' EXIT

        while true; do
            source_secret_name="$(
                select_first_present_secret \
                    "${namespace}" \
                    "${runtime_bundle_secret_name}" \
                    "${bundle_secret_name}" \
                    || true
            )"
            if [ -n "${source_secret_name}" ] \
                && k8s_secret_data_field "${namespace}" "${source_secret_name}" ca.pem > "${bundle_dir}/ca.pem" \
                && k8s_secret_data_field "${namespace}" "${source_secret_name}" crl.pem > "${bundle_dir}/crl.pem"
            then
                local needs_sync=0

                merge_control_plane_crl_bundle \
                    "${runtime_crl_path}" \
                    "${bundle_dir}/crl.pem" \
                    /etc/puppetlabs/puppetserver/ca/ca_crl.pem \
                    /etc/puppetlabs/puppetserver/ca/infra_crl.pem

                current_fingerprint="$(
                    {
                        printf '%s\n' "${source_secret_name}"
                        cat "${bundle_dir}/ca.pem" "${runtime_crl_path}"
                    } | sha256sum | awk '{print $1}'
                )"
                if [ "${current_fingerprint}" != "${fingerprint}" ]; then
                    needs_sync=1
                fi
                if [ "${source_secret_name}" != "${active_secret_name}" ]; then
                    needs_sync=1
                fi
                if ! control_plane_ca_bundle_target_matches \
                    /etc/puppetlabs/puppet/ssl/certs/ca.pem \
                    "${bundle_dir}/ca.pem" \
                    644 \
                    pe-puppet:pe-puppet
                then
                    needs_sync=1
                fi
                if ! control_plane_ca_bundle_target_matches \
                    /etc/puppetlabs/puppetserver/ca/ca_crt.pem \
                    "${bundle_dir}/ca.pem" \
                    640 \
                    pe-puppet:pe-puppet
                then
                    needs_sync=1
                fi
                if ! control_plane_ca_bundle_target_matches \
                    /etc/puppetlabs/puppet/ssl/crl.pem \
                    "${runtime_crl_path}" \
                    644 \
                    pe-puppet:pe-puppet
                then
                    needs_sync=1
                fi
                if ! control_plane_ca_bundle_target_matches \
                    "$(control_plane_hostcrl_path)" \
                    "${runtime_crl_path}" \
                    644 \
                    pe-puppet:pe-puppet
                then
                    needs_sync=1
                fi
                if [ "${needs_sync}" -eq 1 ]; then
                    sync_control_plane_trust_bundle_runtime_from_dir "${bundle_dir}" "${runtime_crl_path}"
                    fingerprint="${current_fingerprint}"
                    active_secret_name="${source_secret_name}"
                    log "Synchronized control-plane trust bundle from Secret ${namespace}/${source_secret_name}"
                fi
            fi
            sleep "${poll_seconds}"
        done
    ) &

    CONTROL_PLANE_CA_BUNDLE_SYNC_PID=$!
}

stop_puppetserver_child() {
    local signal="${1:-TERM}"

    [ -n "${PUPPETSERVER_CHILD_PID}" ] || return 0
    if kill -0 "${PUPPETSERVER_CHILD_PID}" 2>/dev/null; then
        kill "-${signal}" "${PUPPETSERVER_CHILD_PID}" 2>/dev/null || true
        wait "${PUPPETSERVER_CHILD_PID}" 2>/dev/null || true
    fi
    PUPPETSERVER_CHILD_PID=""
}

start_puppetserver_child() {
    if [ "$(id -u)" -eq 0 ]; then
        prepare_user_env pe-puppet
        runuser --preserve-environment -u pe-puppet -- \
            /opt/puppetlabs/server/apps/puppetserver/bin/puppetserver \
            foreground &
    else
        /opt/puppetlabs/server/apps/puppetserver/bin/puppetserver \
            foreground &
    fi

    PUPPETSERVER_CHILD_PID=$!
}

run_puppetserver_supervised() {
    local rc

    trap 'stop_control_plane_ca_bundle_sync_loop; stop_puppetserver_child TERM; exit 143' TERM INT

    sync_control_plane_ca_bundle_once
    sync_control_plane_hostcrl_setting
    start_control_plane_ca_bundle_sync_loop
    start_puppetserver_child

    wait "${PUPPETSERVER_CHILD_PID}"
    rc=$?
    PUPPETSERVER_CHILD_PID=""
    stop_control_plane_ca_bundle_sync_loop
    return "${rc}"
}

orchestration_secret_fingerprint() {
    local path
    local summary=""

    for path in \
        "${PE_K8S_ORCHESTRATION_INVENTORY_KEYS_PATH:-/etc/puppetlabs/orchestration-services/conf.d/secrets/keys.json}" \
        "${PE_K8S_ORCHESTRATION_ENCRYPTION_STORE_PATH:-/etc/puppetlabs/orchestration-services/conf.d/secrets/orchestrator-encryption-keys.json}"
    do
        if [ -f "${path}" ]; then
            summary+="${path}:$(sha256sum "${path}" | awk '{print $1}')"$'\n'
        else
            summary+="${path}:missing"$'\n'
        fi
    done

    printf '%s' "${summary}" | sha256sum | awk '{print $1}'
}

ORCHESTRATION_CHILD_PID=""

stop_orchestration_child() {
    local signal="${1:-TERM}"

    [ -n "${ORCHESTRATION_CHILD_PID}" ] || return 0
    if kill -0 "${ORCHESTRATION_CHILD_PID}" 2>/dev/null; then
        kill "-${signal}" "${ORCHESTRATION_CHILD_PID}" 2>/dev/null || true
        wait "${ORCHESTRATION_CHILD_PID}" 2>/dev/null || true
    fi
    ORCHESTRATION_CHILD_PID=""
}

start_orchestration_child() {
    if [ "$(id -u)" -eq 0 ]; then
        prepare_user_env pe-orchestration-services
        runuser --preserve-environment -u pe-orchestration-services -- \
            /opt/puppetlabs/server/apps/orchestration-services/bin/orchestration-services \
            foreground &
    else
        /opt/puppetlabs/server/apps/orchestration-services/bin/orchestration-services \
            foreground &
    fi
    ORCHESTRATION_CHILD_PID=$!
}

run_orchestration_services_supervised() {
    local expected_fingerprint current_fingerprint rc

    trap 'stop_orchestration_child TERM; exit 143' TERM INT

    while true; do
        expected_fingerprint="$(orchestration_secret_fingerprint)"
        start_orchestration_child

        while kill -0 "${ORCHESTRATION_CHILD_PID}" 2>/dev/null; do
            sleep 2
            current_fingerprint="$(orchestration_secret_fingerprint)"
            if [ "${current_fingerprint}" != "${expected_fingerprint}" ]; then
                log "Detected orchestration encryption material change; restarting orchestration-services"
                stop_orchestration_child TERM
                break
            fi
        done

        if [ -n "${ORCHESTRATION_CHILD_PID}" ]; then
            wait "${ORCHESTRATION_CHILD_PID}"
            rc=$?
            ORCHESTRATION_CHILD_PID=""
            return "${rc}"
        fi

        sleep 1
    done
}

case "${role}" in
    postgresql)
        PGDATA="${PGDATA:-/opt/puppetlabs/server/data/postgresql/14/data}"
        PGPORT="${PGPORT:-5432}"
        ensure_dir /var/log/puppetlabs/postgresql/14
        clear_stale_postgresql_state "${PGDATA}" "${PGPORT}"
        exec_as_user pe-postgres \
            /opt/puppetlabs/server/apps/postgresql/14/bin/postgres \
            -D "${PGDATA}" \
            -c "log_directory=/var/log/puppetlabs/postgresql/14" \
            -p "${PGPORT}"
        ;;
    puppetdb)
        wait_for_postgresql
        exec_as_user pe-puppetdb \
            /opt/puppetlabs/server/apps/puppetdb/bin/puppetdb \
            foreground
        ;;
    puppetserver)
        run_puppetserver_supervised
        ;;
    nginx)
        patch_nginx_ingress_redirects
        exec /opt/puppetlabs/server/bin/nginx \
            -g 'daemon off;' \
            -c /etc/puppetlabs/nginx/nginx.conf
        ;;
    console-services)
        patch_conductor_console_auth_barrier_ports
        sync_conductor_console_auth_shared_state
        sync_orchestration_service_urls
        exec_as_user pe-console-services \
            /opt/puppetlabs/server/apps/console-services/bin/console-services \
            foreground
        ;;
    orchestration-services)
        sync_control_plane_ca_bundle_once
        sync_control_plane_hostcrl_setting
        patch_orchestrator_pcp_broker_allowlist
        patch_local_pcp_controller_uri
        sync_orchestration_listener_ssl_material
        run_orchestration_services_supervised
        ;;
    host-action-collector)
        sync_orchestration_service_urls
        exec_as_user pe-host-action-collector \
            /opt/puppetlabs/server/apps/host-action-collector/bin/host-action-collector \
            foreground
        ;;
    bolt-server)
        sync_orchestration_service_urls
        export GEM_PATH=/opt/puppetlabs/server/apps/bolt-server/lib/ruby/gems/3.2.0
        export GEM_HOME=/opt/puppetlabs/server/apps/bolt-server/lib/ruby/gems/3.2.0
        export APP_ENV=production
        exec_as_user pe-bolt-server \
            /opt/puppetlabs/server/apps/bolt-server/bin/puma \
            -C /opt/puppetlabs/server/apps/bolt-server/config/pe_bolt_server_config.rb \
            -e production
        ;;
    ace-server)
        export GEM_PATH=/opt/puppetlabs/server/apps/ace-server/lib/ruby:/opt/puppetlabs/server/apps/bolt-server/lib/ruby:/opt/puppetlabs/server/apps/bolt-server/lib/ruby/gems/3.2.0
        export GEM_HOME=/opt/puppetlabs/server/apps/ace-server/lib/ruby:/opt/puppetlabs/server/apps/bolt-server/lib/ruby:/opt/puppetlabs/server/apps/bolt-server/lib/ruby/gems/3.2.0
        export APP_ENV=production
        exec_as_user pe-ace-server \
            /opt/puppetlabs/server/apps/ace-server/bin/puma \
            -C /opt/puppetlabs/server/apps/ace-server/config/transport_tasks_config.rb \
            -e production
        ;;
    *)
        log "Unknown PE role: ${role}"
        exit 1
        ;;
esac
