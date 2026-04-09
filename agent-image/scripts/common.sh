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
    ensure_dir /var/run/puppetlabs
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
    if [ -x /opt/puppetlabs/bin/puppet ]; then
        return 0
    fi

    log "Installing puppet-agent from ${PE_AGENT_PACKAGE_REPO_URL}"
    install_package_repo
    dnf install -y puppet-agent procps-ng
    dnf clean all
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
