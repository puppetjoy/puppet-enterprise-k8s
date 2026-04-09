#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

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
fi
ensure_role_runtime_dirs

case "${role}" in
    postgresql)
        PGDATA="${PGDATA:-/opt/puppetlabs/server/data/postgresql/14/data}"
        PGPORT="${PGPORT:-5432}"
        ensure_dir /var/log/puppetlabs/postgresql/14
        exec_as_user pe-postgres \
            /opt/puppetlabs/server/apps/postgresql/14/bin/postgres \
            -D "${PGDATA}" \
            -c "log_directory=/var/log/puppetlabs/postgresql/14" \
            -p "${PGPORT}"
        ;;
    puppetdb)
        exec_as_user pe-puppetdb \
            /opt/puppetlabs/server/apps/puppetdb/bin/puppetdb \
            foreground
        ;;
    puppetserver)
        exec_as_user pe-puppet \
            /opt/puppetlabs/server/apps/puppetserver/bin/puppetserver \
            foreground
        ;;
    nginx)
        patch_nginx_ingress_redirects
        exec /opt/puppetlabs/server/bin/nginx \
            -g 'daemon off;' \
            -c /etc/puppetlabs/nginx/nginx.conf
        ;;
    console-services)
        exec_as_user pe-console-services \
            /opt/puppetlabs/server/apps/console-services/bin/console-services \
            foreground
        ;;
    orchestration-services)
        patch_local_pcp_controller_uri
        exec_as_user pe-orchestration-services \
            /opt/puppetlabs/server/apps/orchestration-services/bin/orchestration-services \
            foreground
        ;;
    host-action-collector)
        exec_as_user pe-host-action-collector \
            /opt/puppetlabs/server/apps/host-action-collector/bin/host-action-collector \
            foreground
        ;;
    bolt-server)
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
