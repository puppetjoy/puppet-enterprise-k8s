#!/bin/bash
set -euo pipefail

PE_AGENT_PACKAGE_REPO_URL="${PE_AGENT_PACKAGE_REPO_URL:-}"
PE_AGENT_PACKAGE_REPO_FILE="${PE_AGENT_PACKAGE_REPO_FILE:-/etc/yum.repos.d/pe-k8s-agent.repo}"
PE_AGENT_PACKAGE_REPO_SSLVERIFY="${PE_AGENT_PACKAGE_REPO_SSLVERIFY:-false}"
PE_AGENT_CERTNAME="${PE_AGENT_CERTNAME:-}"
PE_AGENT_CERTNAME_SUFFIX="${PE_AGENT_CERTNAME_SUFFIX:-.test.puppet}"
PE_AGENT_SERVER="${PE_AGENT_SERVER:-pe}"
PE_AGENT_SERVER_LIST="${PE_AGENT_SERVER_LIST:-}"
PE_AGENT_CA_SERVER="${PE_AGENT_CA_SERVER:-${PE_AGENT_SERVER}}"
PE_AGENT_ENVIRONMENT="${PE_AGENT_ENVIRONMENT:-production}"
PE_AGENT_RUNINTERVAL="${PE_AGENT_RUNINTERVAL:-30m}"
PE_AGENT_WAITFORCERT="${PE_AGENT_WAITFORCERT:-60}"
PE_AGENT_SPLAY="${PE_AGENT_SPLAY:-true}"
PE_AGENT_NODE_NAME="${PE_AGENT_NODE_NAME:-${HOSTNAME:-agent}}"
PE_AGENT_PXP_ENABLED="${PE_AGENT_PXP_ENABLED:-true}"
PE_AGENT_PXP_LOGLEVEL="${PE_AGENT_PXP_LOGLEVEL:-info}"
PE_AGENT_PXP_CONFIG="${PE_AGENT_PXP_CONFIG:-/etc/puppetlabs/pxp-agent/pxp-agent.conf}"
PE_AGENT_PXP_PIDFILE="${PE_AGENT_PXP_PIDFILE:-/var/run/puppetlabs/pxp-agent.pid}"

log() {
    printf '[pe-agent] %s\n' "$*"
}

ensure_dir() {
    mkdir -p "$1"
}

agent_certname() {
    if [ -n "${PE_AGENT_CERTNAME}" ]; then
        printf '%s\n' "${PE_AGENT_CERTNAME}"
    else
        printf '%s%s\n' "${PE_AGENT_NODE_NAME}" "${PE_AGENT_CERTNAME_SUFFIX}"
    fi
}

ensure_agent_directories() {
    ensure_dir /etc/puppetlabs/puppet
    ensure_dir /opt/puppetlabs/puppet/cache
    ensure_dir /var/log/puppetlabs/puppet
    ensure_dir /var/log/puppetlabs/pxp-agent
    ensure_dir /var/run/puppetlabs
}

pxp_agent_enabled() {
    case "${PE_AGENT_PXP_ENABLED}" in
        true|TRUE|1|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

pxp_agent_configured() {
    [ -f "${PE_AGENT_PXP_CONFIG}" ]
}

pxp_agent_pid() {
    if [ -f "${PE_AGENT_PXP_PIDFILE}" ]; then
        cat "${PE_AGENT_PXP_PIDFILE}"
        return 0
    fi

    pgrep -f '^/opt/puppetlabs/puppet/bin/pxp-agent($| )' | head -n 1 || true
}

pxp_agent_running() {
    local pid

    pid="$(pxp_agent_pid)"
    [ -n "${pid}" ] || return 1
    kill -0 "${pid}" >/dev/null 2>&1
}

install_service_shims() {
    local service_name

    ensure_dir /etc/init.d

    for service_name in puppet pxp-agent; do
        if [ ! -e "/etc/init.d/${service_name}" ]; then
            cat > "/etc/init.d/${service_name}" <<EOF
#!/bin/sh
exec /usr/local/bin/pe-agent-servicectl "\${1:-status}" "${service_name}"
EOF
        fi

        chmod 0755 "/etc/init.d/${service_name}"
    done
}

start_pxp_agent() {
    if ! pxp_agent_enabled; then
        return 0
    fi

    if ! pxp_agent_configured; then
        log "PXP agent config is missing; skipping startup"
        return 1
    fi

    if pxp_agent_running; then
        return 0
    fi

    ensure_agent_directories

    log "Starting pxp-agent for $(agent_certname)"
    /opt/puppetlabs/bin/pxp-agent \
        --config-file "${PE_AGENT_PXP_CONFIG}" \
        --loglevel "${PE_AGENT_PXP_LOGLEVEL}" \
        --pidfile "${PE_AGENT_PXP_PIDFILE}"

    sleep 1
    pxp_agent_running
}

stop_pxp_agent() {
    local pid
    local deadline

    pid="$(pxp_agent_pid)"
    [ -n "${pid}" ] || return 0

    kill "${pid}" >/dev/null 2>&1 || true
    deadline=$((SECONDS + 10))
    while kill -0 "${pid}" >/dev/null 2>&1; do
        if [ "${SECONDS}" -ge "${deadline}" ]; then
            kill -9 "${pid}" >/dev/null 2>&1 || true
            break
        fi
        sleep 1
    done

    rm -f "${PE_AGENT_PXP_PIDFILE}"
}

install_package_repo() {
    [ -n "${PE_AGENT_PACKAGE_REPO_URL}" ] || {
        log "PE_AGENT_PACKAGE_REPO_URL is required"
        return 1
    }

    ensure_dir "$(dirname "${PE_AGENT_PACKAGE_REPO_FILE}")"
    curl -sk "${PE_AGENT_PACKAGE_REPO_URL}" -o "${PE_AGENT_PACKAGE_REPO_FILE}"

    if grep -Fqx 'proxy=$PROXY' "${PE_AGENT_PACKAGE_REPO_FILE}"; then
        sed -i 's/^proxy=\$PROXY$/proxy=_none_/' "${PE_AGENT_PACKAGE_REPO_FILE}"
    fi

    if [ "${PE_AGENT_PACKAGE_REPO_SSLVERIFY}" = "true" ]; then
        if grep -q '^sslverify=' "${PE_AGENT_PACKAGE_REPO_FILE}"; then
            sed -i 's/^sslverify=.*/sslverify=1/' "${PE_AGENT_PACKAGE_REPO_FILE}"
        fi
    else
        if grep -q '^sslverify=' "${PE_AGENT_PACKAGE_REPO_FILE}"; then
            sed -i 's/^sslverify=.*/sslverify=0/' "${PE_AGENT_PACKAGE_REPO_FILE}"
        else
            printf '%s\n' 'sslverify=0' >> "${PE_AGENT_PACKAGE_REPO_FILE}"
        fi
    fi
}

ensure_puppet_agent_installed() {
    if [ ! -x /opt/puppetlabs/bin/puppet ]; then
        log "Installing puppet-agent from ${PE_AGENT_PACKAGE_REPO_URL}"
        install_package_repo
        dnf install -y puppet-agent procps-ng
        dnf clean all
    fi

    install_service_shims
}

write_puppet_conf() {
    local certname
    local server_list_line=""
    certname="$(agent_certname)"

    if [ -n "${PE_AGENT_SERVER_LIST}" ]; then
        server_list_line="server_list = ${PE_AGENT_SERVER_LIST}"
    fi

    ensure_agent_directories

    cat > /etc/puppetlabs/puppet/puppet.conf <<EOF
[main]
certname = ${certname}
server = ${PE_AGENT_SERVER}
ca_server = ${PE_AGENT_CA_SERVER}
environment = ${PE_AGENT_ENVIRONMENT}
runinterval = ${PE_AGENT_RUNINTERVAL}
usecacheonfailure = false
splay = ${PE_AGENT_SPLAY}
vardir = /opt/puppetlabs/puppet/cache
logdir = /var/log/puppetlabs/puppet
rundir = /var/run/puppetlabs
${server_list_line}

[agent]
pluginsync = true
report = true
waitforcert = ${PE_AGENT_WAITFORCERT}
EOF
}

agent_certificate_ready() {
    local certname
    certname="$(agent_certname)"
    [ -x /opt/puppetlabs/bin/puppet ] || return 1
    [ -f "/etc/puppetlabs/puppet/ssl/certs/${certname}.pem" ] || return 1
    /opt/puppetlabs/bin/puppet ssl verify >/dev/null 2>&1
}

bootstrap_agent_ssl() {
    if agent_certificate_ready; then
        return 0
    fi

    log "Bootstrapping SSL for $(agent_certname)"
    /opt/puppetlabs/bin/puppet ssl bootstrap \
        --certname "$(agent_certname)" \
        --ca_server "${PE_AGENT_CA_SERVER}"
}

run_initial_agent_convergence() {
    local rc

    log "Running initial puppet agent convergence for $(agent_certname)"
    set +e
    /opt/puppetlabs/bin/puppet agent \
        --test \
        --onetime \
        --no-daemonize \
        --no-splay \
        --verbose
    rc=$?
    set -e

    case "${rc}" in
        0|2)
            return 0
            ;;
        *)
            log "Initial puppet agent run exited with status ${rc}; continuing with long-running agent"
            return 0
            ;;
    esac
}
