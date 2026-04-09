#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_K8S_SERVICE_STATE_DIR="${PE_K8S_SERVICE_STATE_DIR:-${PE_K8S_STATE_DIR}/services}"
PE_K8S_SERVICE_LOG_DIR="${PE_K8S_SERVICE_LOG_DIR:-/var/log/puppetlabs/service-manager}"

log_sm() {
    printf '[pe-k8s-service] %s\n' "$*" >&2
}

canonical_service_name() {
    local name="${1:-}"
    name="${name##*/}"
    name="${name%.service}"
    printf '%s\n' "${name}"
}

service_pid_file() {
    printf '%s/%s.pid\n' "${PE_K8S_SERVICE_STATE_DIR}" "$(canonical_service_name "$1")"
}

service_enabled_file() {
    printf '%s/%s.enabled\n' "${PE_K8S_SERVICE_STATE_DIR}" "$(canonical_service_name "$1")"
}

service_log_file() {
    printf '%s/%s.log\n' "${PE_K8S_SERVICE_LOG_DIR}" "$(canonical_service_name "$1")"
}

service_user() {
    local service_name
    service_name="$(canonical_service_name "$1")"
    case "${service_name}" in
        pe-puppetserver) printf '%s\n' pe-puppet ;;
        pe-puppetdb) printf '%s\n' pe-puppetdb ;;
        pe-console-services) printf '%s\n' pe-console-services ;;
        pe-orchestration-services) printf '%s\n' pe-orchestration-services ;;
        pe-host-action-collector) printf '%s\n' pe-host-action-collector ;;
        pe-infra-assistant) printf '%s\n' pe-infra-assistant ;;
        pe-workflow-service) printf '%s\n' pe-workflow-service ;;
        pe-patching-service) printf '%s\n' pe-patching-service ;;
        pe-postgresql) printf '%s\n' pe-postgres ;;
        pe-bolt-server) printf '%s\n' pe-bolt-server ;;
        pe-ace-server) printf '%s\n' pe-ace-server ;;
        pe-nginx) printf '%s\n' pe-webserver ;;
        pxp-agent) printf '%s\n' pe-puppet ;;
        puppet) printf '%s\n' pe-puppet ;;
        *)
            return 1
            ;;
    esac
}

service_group() {
    service_user "$1"
}

service_runtime_log_dirs() {
    local service_name
    service_name="$(canonical_service_name "$1")"
    case "${service_name}" in
        pe-puppetserver)
            printf '%s\n' /var/log/puppetlabs/puppetserver
            ;;
        pe-puppetdb)
            printf '%s\n' /var/log/puppetlabs/puppetdb
            ;;
        pe-console-services)
            printf '%s\n' /var/log/puppetlabs/console-services
            ;;
        pe-orchestration-services)
            printf '%s\n' /var/log/puppetlabs/orchestration-services
            ;;
        pe-host-action-collector)
            printf '%s\n' /var/log/puppetlabs/host-action-collector
            ;;
        pe-infra-assistant)
            printf '%s\n' /var/log/puppetlabs/infra-assistant
            ;;
        pe-workflow-service)
            printf '%s\n' /var/log/puppetlabs/workflow-service
            ;;
        pe-patching-service)
            printf '%s\n' /var/log/puppetlabs/patching-service
            ;;
        pe-postgresql)
            printf '%s\n' /var/log/puppetlabs/postgresql
            printf '%s\n' /var/log/puppetlabs/postgresql/14
            ;;
        pe-bolt-server)
            printf '%s\n' /var/log/puppetlabs/bolt-server
            ;;
        pe-ace-server)
            printf '%s\n' /var/log/puppetlabs/ace-server
            ;;
        pe-nginx)
            printf '%s\n' /var/log/puppetlabs/nginx
            ;;
        pxp-agent)
            printf '%s\n' /var/log/puppetlabs/pxp-agent
            ;;
        puppet)
            printf '%s\n' /var/log/puppetlabs/puppet
            ;;
    esac
}

service_app_name() {
    local service_name
    service_name="$(canonical_service_name "$1")"
    printf '%s\n' "${service_name#pe-}"
}

service_listen_port() {
    local service_name
    service_name="$(canonical_service_name "$1")"
    case "${service_name}" in
        pe-nginx) printf '%s\n' 443 ;;
        pe-postgresql) printf '%s\n' 5432 ;;
        pe-bolt-server) printf '%s\n' 62658 ;;
        pe-ace-server) printf '%s\n' 44633 ;;
        pe-puppetserver) printf '%s\n' 8140 ;;
        pe-puppetdb) printf '%s\n' 8081 ;;
        pe-console-services) printf '%s\n' 4433 ;;
        pe-orchestration-services) printf '%s\n' 8143 ;;
        pe-host-action-collector) printf '%s\n' 8147 ;;
        *)
            return 1
            ;;
    esac
}

service_start_timeout() {
    local service_name
    service_name="$(canonical_service_name "$1")"
    case "${service_name}" in
        pe-puppetserver) printf '%s\n' 90 ;;
        pe-puppetdb|pe-console-services|pe-orchestration-services) printf '%s\n' 45 ;;
        pe-postgresql|pe-bolt-server|pe-ace-server|pe-host-action-collector|pe-nginx) printf '%s\n' 30 ;;
        *)
            printf '%s\n' 5
            ;;
    esac
}

wait_for_tcp_port() {
    local port="$1"
    if exec 3<>"/dev/tcp/127.0.0.1/${port}" 2>/dev/null; then
        exec 3>&-
        exec 3<&-
        return 0
    fi
    return 1
}

wait_for_service_ready() {
    local service_name pid port timeout elapsed
    service_name="$(canonical_service_name "$1")"
    pid="$2"
    port="$(service_listen_port "${service_name}" 2>/dev/null || true)"
    [ -n "${port}" ] || return 0

    timeout="$(service_start_timeout "${service_name}")"
    elapsed=0
    while [ "${elapsed}" -lt "${timeout}" ]; do
        if ! kill -0 "${pid}" 2>/dev/null; then
            return 1
        fi
        if wait_for_tcp_port "${port}"; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done

    return 1
}

generic_service_bin() {
    local app
    app="$(service_app_name "$1")"
    local bin="/opt/puppetlabs/server/apps/${app}/bin/${app}"
    if [ -x "${bin}" ]; then
        printf '%s\n' "${bin}"
        return 0
    fi
    return 1
}

known_services() {
    local name bin
    printf '%s\n' pe-nginx pe-postgresql pe-bolt-server pe-ace-server puppet pxp-agent
    if [ -d /opt/puppetlabs/server/apps ]; then
        find /opt/puppetlabs/server/apps -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | while IFS= read -r name; do
            bin="/opt/puppetlabs/server/apps/${name}/bin/${name}"
            if [ -x "${bin}" ]; then
                printf 'pe-%s\n' "${name}"
            fi
        done
    fi
}

service_known() {
    local service_name
    service_name="$(canonical_service_name "$1")"
    case "${service_name}" in
        pe-nginx|pe-postgresql|pe-bolt-server|pe-ace-server|puppet|pxp-agent)
            return 0
            ;;
    esac
    generic_service_bin "${service_name}" >/dev/null 2>&1
}

ensure_service_dirs() {
    ensure_dir "${PE_K8S_SERVICE_STATE_DIR}"
    ensure_dir "${PE_K8S_SERVICE_LOG_DIR}"
}

ensure_service_runtime_dirs() {
    local service_name user group dir
    service_name="$(canonical_service_name "$1")"
    user="$(service_user "${service_name}" 2>/dev/null || true)"
    group="$(service_group "${service_name}" 2>/dev/null || true)"

    while IFS= read -r dir; do
        [ -n "${dir}" ] || continue
        ensure_dir "${dir}"
        if [ -n "${user}" ] && [ -n "${group}" ]; then
            chown "${user}:${group}" "${dir}"
        fi
        chmod 0750 "${dir}" || true
    done < <(service_runtime_log_dirs "${service_name}")
}

service_enabled() {
    local state_file
    state_file="$(service_enabled_file "$1")"
    if [ -f "${state_file}" ]; then
        grep -qx 'enabled' "${state_file}"
        return
    fi
    return 0
}

set_service_enabled_state() {
    local state="$1"
    local state_file
    state_file="$(service_enabled_file "$2")"
    ensure_service_dirs
    printf '%s\n' "${state}" > "${state_file}"
}

service_pid() {
    local pid_file
    pid_file="$(service_pid_file "$1")"
    [ -f "${pid_file}" ] || return 1
    cat "${pid_file}"
}

service_active() {
    local pid
    pid="$(service_pid "$1")" || return 1
    kill -0 "${pid}" 2>/dev/null
}

launch_special_service() {
    local service_name="$1"
    case "${service_name}" in
        pe-nginx)
            exec /opt/puppetlabs/server/bin/nginx -g 'daemon off;' -c /etc/puppetlabs/nginx/nginx.conf
            ;;
        pe-postgresql)
            ensure_dir /var/log/puppetlabs/postgresql/14
            prepare_user_env pe-postgres
            exec runuser --preserve-environment -u pe-postgres -- \
                /opt/puppetlabs/server/apps/postgresql/14/bin/postgres \
                -D "${PGDATA:-/opt/puppetlabs/server/data/postgresql/14/data}" \
                -c "log_directory=/var/log/puppetlabs/postgresql/14" \
                -p "${PGPORT:-5432}"
            ;;
        pe-bolt-server)
            export GEM_PATH=/opt/puppetlabs/server/apps/bolt-server/lib/ruby/gems/3.2.0
            export GEM_HOME=/opt/puppetlabs/server/apps/bolt-server/lib/ruby/gems/3.2.0
            export APP_ENV=production
            prepare_user_env pe-bolt-server
            exec runuser --preserve-environment -u pe-bolt-server -- \
                /opt/puppetlabs/server/apps/bolt-server/bin/puma \
                -C /opt/puppetlabs/server/apps/bolt-server/config/pe_bolt_server_config.rb \
                -e production
            ;;
        pe-ace-server)
            export GEM_PATH=/opt/puppetlabs/server/apps/ace-server/lib/ruby:/opt/puppetlabs/server/apps/bolt-server/lib/ruby:/opt/puppetlabs/server/apps/bolt-server/lib/ruby/gems/3.2.0
            export GEM_HOME=/opt/puppetlabs/server/apps/ace-server/lib/ruby:/opt/puppetlabs/server/apps/bolt-server/lib/ruby:/opt/puppetlabs/server/apps/bolt-server/lib/ruby/gems/3.2.0
            export APP_ENV=production
            prepare_user_env pe-ace-server
            exec runuser --preserve-environment -u pe-ace-server -- \
                /opt/puppetlabs/server/apps/ace-server/bin/puma \
                -C /opt/puppetlabs/server/apps/ace-server/config/transport_tasks_config.rb \
                -e production
            ;;
        puppet|pxp-agent)
            exec bash -lc 'trap "exit 0" TERM INT; while :; do sleep 3600; done'
            ;;
    esac
    return 1
}

launch_service_foreground() {
    local service_name
    service_name="$(canonical_service_name "$1")"

    copy_exported_sysconfig_into_rootfs

    if launch_special_service "${service_name}"; then
        return 0
    fi

    local bin
    bin="$(generic_service_bin "${service_name}")" || {
        log_sm "Unknown service ${service_name}"
        return 1
    }
    exec "${bin}" foreground
}

start_service() {
    local service_name log_file pid_file pid
    service_name="$(canonical_service_name "$1")"
    service_known "${service_name}" || {
        log_sm "Refusing to start unknown service ${service_name}"
        return 1
    }

    if service_active "${service_name}"; then
        return 0
    fi

    ensure_service_dirs
    ensure_service_runtime_dirs "${service_name}"
    log_file="$(service_log_file "${service_name}")"
    pid_file="$(service_pid_file "${service_name}")"

    (
        exec >>"${log_file}" 2>&1
        launch_service_foreground "${service_name}"
    ) &
    pid=$!
    printf '%s\n' "${pid}" > "${pid_file}"

    sleep 1
    if ! kill -0 "${pid}" 2>/dev/null; then
        log_sm "Service ${service_name} failed to stay up"
        tail -n 80 "${log_file}" >&2 || true
        return 1
    fi

    if ! wait_for_service_ready "${service_name}" "${pid}"; then
        log_sm "Service ${service_name} did not become ready"
        tail -n 80 "${log_file}" >&2 || true
        stop_service "${service_name}" || true
        return 1
    fi
}

stop_service() {
    local service_name pid pid_file
    service_name="$(canonical_service_name "$1")"
    pid_file="$(service_pid_file "${service_name}")"
    pid="$(service_pid "${service_name}")" || {
        rm -f "${pid_file}"
        return 0
    }

    kill "${pid}" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        if ! kill -0 "${pid}" 2>/dev/null; then
            rm -f "${pid_file}"
            return 0
        fi
        sleep 1
    done
    kill -9 "${pid}" 2>/dev/null || true
    rm -f "${pid_file}"
}

status_service() {
    local service_name
    service_name="$(canonical_service_name "$1")"
    if service_active "${service_name}"; then
        printf 'active\n'
        return 0
    fi
    printf 'inactive\n'
    return 3
}

do_service_command() {
    local action="$1"
    local service_name="$2"
    case "${action}" in
        start)
            start_service "${service_name}"
            ;;
        stop)
            stop_service "${service_name}"
            ;;
        restart|reload)
            stop_service "${service_name}"
            start_service "${service_name}"
            ;;
        status|is-active)
            status_service "${service_name}"
            ;;
        *)
            log_sm "Unsupported service action ${action}"
            return 1
            ;;
    esac
}

handle_service_wrapper() {
    local service_name="${1:-}"
    local action="${2:-status}"
    [ -n "${service_name}" ] || {
        log_sm "service wrapper requires a service name"
        exit 1
    }
    do_service_command "${action}" "${service_name}"
}

handle_chkconfig_wrapper() {
    local args=("$@")
    local service_name state

    if [ "${#args[@]}" -eq 1 ]; then
        service_name="$(canonical_service_name "${args[0]}")"
        if service_enabled "${service_name}"; then
            printf '%s\t%s\n' "${service_name}" on
            exit 0
        fi
        printf '%s\t%s\n' "${service_name}" off
        exit 1
    fi

    if [ "${args[0]}" = "--add" ]; then
        service_name="$(canonical_service_name "${args[1]}")"
        set_service_enabled_state enabled "${service_name}"
        exit 0
    fi

    if [ "${args[0]}" = "--level" ]; then
        service_name="$(canonical_service_name "${args[2]}")"
        state="${args[3]}"
    else
        service_name="$(canonical_service_name "${args[0]}")"
        state="${args[1]}"
    fi

    case "${state}" in
        on)
            set_service_enabled_state enabled "${service_name}"
            ;;
        off)
            set_service_enabled_state disabled "${service_name}"
            ;;
        *)
            log_sm "Unsupported chkconfig state ${state}"
            exit 1
            ;;
    esac
}

handle_systemctl_wrapper() {
    while [ "${#}" -gt 0 ]; do
        case "${1}" in
            --no-reload|--quiet|--no-ask-password)
                shift
                ;;
            --)
                shift
                break
                ;;
            *)
                break
                ;;
        esac
    done

    local command="${1:-}"
    shift || true

    case "${command}" in
        list-unit-files)
            while IFS= read -r service_name; do
                [ -n "${service_name}" ] || continue
                if service_enabled "${service_name}"; then
                    printf '%s.service enabled\n' "${service_name}"
                else
                    printf '%s.service disabled\n' "${service_name}"
                fi
            done < <(known_services | sort -u)
            ;;
        is-enabled)
            [ "${1:-}" = "--" ] && shift
            local service_name
            service_name="$(canonical_service_name "${1:-}")"
            if service_enabled "${service_name}"; then
                printf 'enabled\n'
                exit 0
            fi
            printf 'disabled\n'
            exit 1
            ;;
        enable|disable|mask|unmask)
            [ "${1:-}" = "--" ] && shift
            local service_name
            service_name="$(canonical_service_name "${1:-}")"
            case "${command}" in
                enable|unmask)
                    set_service_enabled_state enabled "${service_name}"
                    ;;
                disable|mask)
                    set_service_enabled_state disabled "${service_name}"
                    ;;
            esac
            ;;
        cat)
            [ "${1:-}" = "--" ] && shift
            local service_name
            service_name="$(canonical_service_name "${1:-}")"
            service_known "${service_name}" || exit 1
            cat <<EOF
[Unit]
Description=${service_name}

[Service]
Type=simple
EOF
            ;;
        show)
            if [ "${1:-}" = "--property=NeedDaemonReload" ]; then
                printf 'NeedDaemonReload=no\n'
                exit 0
            fi
            printf 'NeedDaemonReload=no\n'
            ;;
        preset|preset-all)
            ;;
        daemon-reload)
            ;;
        start|stop|restart|reload|is-active|status)
            [ "${1:-}" = "--" ] && shift
            do_service_command "${command}" "${1:-}"
            ;;
        *)
            log_sm "Unsupported systemctl command ${command}"
            exit 1
            ;;
    esac
}

main() {
    local argv0
    argv0="$(basename "$0")"

    case "${argv0}" in
        service)
            handle_service_wrapper "$@"
            ;;
        chkconfig)
            handle_chkconfig_wrapper "$@"
            ;;
        systemctl|pe-k8s-servicectl)
            handle_systemctl_wrapper "$@"
            ;;
        *)
            log_sm "Unknown entrypoint ${argv0}"
            exit 1
            ;;
    esac
}

main "$@"
