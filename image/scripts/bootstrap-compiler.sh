#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_COMPILER_CERTNAME="${PE_COMPILER_CERTNAME:-}"
PE_COMPILER_POD_NAME="${PE_COMPILER_POD_NAME:-${HOSTNAME:-}}"
PE_COMPILER_NAMESPACE="${PE_COMPILER_NAMESPACE:-default}"
PE_COMPILER_HEADLESS_SERVICE="${PE_COMPILER_HEADLESS_SERVICE:-}"
PE_COMPILER_PE_SERVICE="${PE_COMPILER_PE_SERVICE:-pe}"
PE_COMPILER_PE_CERTNAME="${PE_COMPILER_PE_CERTNAME:-${PE_COMPILER_PE_SERVICE}}"
PE_COMPILER_PUPPETDB_HOST="${PE_COMPILER_PUPPETDB_HOST:-${PE_COMPILER_PE_SERVICE}}"
PE_COMPILER_POSTGRESQL_HOST="${PE_COMPILER_POSTGRESQL_HOST:-}"
PE_COMPILER_DNS_ALT_NAMES="${PE_COMPILER_DNS_ALT_NAMES:-}"
PE_COMPILER_PUPPETDB_SYNC_INTERVAL_MINUTES="${PE_COMPILER_PUPPETDB_SYNC_INTERVAL_MINUTES:-5}"
PE_COMPILER_CERT_WAIT_TIMEOUT_SECONDS="${PE_COMPILER_CERT_WAIT_TIMEOUT_SECONDS:-900}"
PE_COMPILER_BOOTSTRAP_DIR="${PE_COMPILER_BOOTSTRAP_DIR:-/etc/puppetlabs/pe-k8s-compiler}"
PE_COMPILER_MANIFEST_PATH="${PE_COMPILER_MANIFEST_PATH:-${PE_COMPILER_BOOTSTRAP_DIR}/bootstrap.pp}"

compiler_certname() {
    if [ -n "${PE_COMPILER_CERTNAME}" ]; then
        printf '%s\n' "${PE_COMPILER_CERTNAME}"
        return 0
    fi

    [ -n "${PE_COMPILER_HEADLESS_SERVICE}" ] || {
        log "PE_COMPILER_HEADLESS_SERVICE is required when PE_COMPILER_CERTNAME is not set"
        exit 1
    }

    printf '%s.%s.%s.svc.cluster.local\n' \
        "${PE_COMPILER_POD_NAME}" \
        "${PE_COMPILER_HEADLESS_SERVICE}" \
        "${PE_COMPILER_NAMESPACE}"
}

compiler_postgresql_host() {
    if [ -n "${PE_COMPILER_POSTGRESQL_HOST}" ]; then
        printf '%s\n' "${PE_COMPILER_POSTGRESQL_HOST}"
        return 0
    fi

    compiler_certname
}

wait_for_pe() {
    wait_for_remote_pe_status "${PE_COMPILER_PE_SERVICE}" "${PE_COMPILER_CERT_WAIT_TIMEOUT_SECONDS}"
}

ensure_compiler_dirs() {
    ensure_dir /etc/puppetlabs/puppet
    ensure_dir /opt/puppetlabs/puppet/cache
    ensure_dir /opt/puppetlabs/server/data
    ensure_dir /var/log/puppetlabs
    ensure_dir /var/log/puppetlabs/puppet
    ensure_dir /var/run/puppetlabs
    ensure_dir "${PE_COMPILER_BOOTSTRAP_DIR}"
}

write_compiler_identity() {
    local certname dns_alt_names
    certname="$(compiler_certname)"
    dns_alt_names="${PE_COMPILER_DNS_ALT_NAMES}"

    cat > /etc/puppetlabs/puppet/puppet.conf <<EOF
[main]
certname = ${certname}
server = ${PE_COMPILER_PE_SERVICE}
ca_server = ${PE_COMPILER_PE_SERVICE}
environment = production
vardir = /opt/puppetlabs/puppet/cache
logdir = /var/log/puppetlabs/puppet
rundir = /var/run/puppetlabs
EOF

    if [ -n "${dns_alt_names}" ]; then
        printf 'dns_alt_names = %s\n' "${dns_alt_names}" >> /etc/puppetlabs/puppet/puppet.conf
    fi

    cat >> /etc/puppetlabs/puppet/puppet.conf <<'EOF'

[agent]
pluginsync = true
report = true
waitforcert = 5
EOF

    cat > /etc/puppetlabs/puppet/csr_attributes.yaml <<'EOF'
---
extension_requests:
  pp_auth_role: pe_compiler
EOF
}

compiler_certificate_ready() {
    local certname
    certname="$(compiler_certname)"
    [ -x /opt/puppetlabs/bin/puppet ] || return 1
    [ -f "/etc/puppetlabs/puppet/ssl/certs/${certname}.pem" ] || return 1
    /opt/puppetlabs/bin/puppet ssl verify >/dev/null 2>&1
}

bootstrap_compiler_ssl() {
    local deadline certname
    certname="$(compiler_certname)"

    if compiler_certificate_ready; then
        return 0
    fi

    deadline=$((SECONDS + PE_COMPILER_CERT_WAIT_TIMEOUT_SECONDS))
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        log "Bootstrapping compiler certificate for ${certname}"
        if /opt/puppetlabs/bin/puppet ssl bootstrap \
            --certname "${certname}" \
            --server "${PE_COMPILER_PE_SERVICE}" \
            --ca_server "${PE_COMPILER_PE_SERVICE}" \
            --waitforcert 5
        then
            return 0
        fi
        sleep 5
    done

    log "Timed out waiting for signed compiler certificate ${certname}"
    return 1
}

write_compiler_manifest() {
    local certname database_host
    certname="$(compiler_certname)"
    database_host="$(compiler_postgresql_host)"

    cat > "${PE_COMPILER_MANIFEST_PATH}" <<EOF
class { 'puppet_enterprise':
  puppet_master_host         => '${PE_COMPILER_PE_SERVICE}',
  certificate_authority_host => '${PE_COMPILER_PE_SERVICE}',
  console_host               => '${PE_COMPILER_PE_SERVICE}',
  puppetdb_host              => ['${certname}', '${PE_COMPILER_PUPPETDB_HOST}'],
  pcp_broker_host            => '${PE_COMPILER_PE_SERVICE}',
}

class { 'puppet_enterprise::profile::master':
  ca_host                     => '${PE_COMPILER_PE_SERVICE}',
  ca_port                     => 8140,
  certname                    => '${certname}',
  classifier_host             => '${PE_COMPILER_PE_SERVICE}',
  classifier_client_certname  => '${PE_COMPILER_PE_CERTNAME}',
  console_host                => '${PE_COMPILER_PE_SERVICE}',
  console_server_certname     => '${PE_COMPILER_PE_CERTNAME}',
  console_client_certname     => '${PE_COMPILER_PE_CERTNAME}',
  master_of_masters_certname  => '${PE_COMPILER_PE_CERTNAME}',
  file_sync_enabled           => true,
  code_manager_auto_configure => false,
  puppetdb_host               => ['${certname}', '${PE_COMPILER_PUPPETDB_HOST}'],
  puppetdb_port               => [8081, 8081],
  enable_patching_service     => false,
  enable_infra_assistant      => false,
  enable_workflow_service     => false,
}

class { 'puppet_enterprise::profile::database':
  certname               => '${database_host}',
  puppetdb_hosts         => ['${certname}'],
  console_hosts          => [],
  pcp_broker_hosts       => [],
  patching_service_hosts => [],
  infra_assistant_hosts  => [],
  workflow_service_hosts => [],
}

class { 'puppet_enterprise::profile::puppetdb':
  database_host   => '${database_host}',
  certname        => '${certname}',
  master_certname => '${certname}',
  rbac_host       => '${PE_COMPILER_PE_SERVICE}',
  sync_peers      => [
    {
      host                  => '${PE_COMPILER_PUPPETDB_HOST}',
      port                  => 8081,
      sync_interval_minutes => ${PE_COMPILER_PUPPETDB_SYNC_INTERVAL_MINUTES},
    }
  ],
  sync_allowlist  => ['${PE_COMPILER_PE_CERTNAME}'],
  require         => Class['puppet_enterprise::profile::database'],
}
EOF
}

stop_bootstrap_services() {
    systemctl stop pe-puppetserver >/dev/null 2>&1 || true
    systemctl stop pe-puppetdb >/dev/null 2>&1 || true
}

run_compiler_apply() {
    local certname rc
    certname="$(compiler_certname)"

    export FACTER_hostname="${PE_COMPILER_POD_NAME}"
    export FACTER_fqdn="${certname}"

    set +e
    /opt/puppetlabs/bin/puppet apply \
        --certname "${certname}" \
        --detailed-exitcodes \
        --modulepath /opt/puppetlabs/puppet/modules \
        "${PE_COMPILER_MANIFEST_PATH}"
    rc=$?
    set -e

    case "${rc}" in
        0|2)
            return 0
            ;;
        *)
            log "Compiler bootstrap apply exited with status ${rc}"
            return "${rc}"
            ;;
    esac
}

main() {
    trap stop_bootstrap_services EXIT

    ensure_compiler_dirs
    wait_for_pe
    write_compiler_identity
    bootstrap_compiler_ssl
    write_compiler_manifest
    run_compiler_apply
    export_runtime_rootfs_artifacts
    log "Compiler bootstrap converged for $(compiler_certname)"
}

main "$@"
