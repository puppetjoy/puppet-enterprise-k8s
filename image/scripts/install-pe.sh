#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_INSTALLER_ROOT="${PE_INSTALLER_ROOT:-/opt/pe-installer}"
PE_CONF_TEMPLATE_PATH="${PE_CONF_TEMPLATE_PATH:-${PE_CONF_PATH:-/config/pe.conf}}"
PE_CONF_PATH="${PE_CONF_PATH:-/config/pe.conf}"
PE_LICENSE_PATH="${PE_LICENSE_PATH:-/license/license.txt}"
PE_INSTALLER_CMD="${PE_INSTALLER_CMD:-${PE_INSTALLER_ROOT}/puppet-enterprise-installer}"
PE_K8S_EXPORT_SUMMARY="${PE_K8S_EXPORT_SUMMARY:-${PE_K8S_INSTALL_DIR}/install-summary.txt}"
PE_K8S_INSTALL_LOG="${PE_K8S_INSTALL_LOG:-${PE_K8S_INSTALL_DIR}/installer-run.log}"
FORCE_REINSTALL="${FORCE_REINSTALL:-false}"
PE_BOOTSTRAP_PUPPET_CERTNAME="${PE_BOOTSTRAP_PUPPET_CERTNAME:-}"
PE_BOOTSTRAP_PUPPET_SERVER="${PE_BOOTSTRAP_PUPPET_SERVER:-}"
PE_CONTROL_PLANE_POD_NAME="${PE_CONTROL_PLANE_POD_NAME:-${HOSTNAME:-}}"
PE_CONTROL_PLANE_NAMESPACE="${PE_CONTROL_PLANE_NAMESPACE:-}"
PE_CONTROL_PLANE_HEADLESS_SERVICE="${PE_CONTROL_PLANE_HEADLESS_SERVICE:-}"
PE_CONTROL_PLANE_CERTNAME="${PE_CONTROL_PLANE_CERTNAME:-}"
PE_CONTROL_PLANE_PUPPET_MASTER_HOST="${PE_CONTROL_PLANE_PUPPET_MASTER_HOST:-${PE_BOOTSTRAP_PUPPET_SERVER:-}}"
PE_CONTROL_PLANE_SHARED_DNS_ALT_NAMES="${PE_CONTROL_PLANE_SHARED_DNS_ALT_NAMES:-}"

start_install_logging() {
    ensure_dir "$(dirname "${PE_K8S_INSTALL_LOG}")"
    touch "${PE_K8S_INSTALL_LOG}"
    exec > >(tee -a "${PE_K8S_INSTALL_LOG}") 2>&1
}

ensure_runtime_mounts() {
    ensure_dir /etc/puppetlabs
    ensure_dir /opt/puppetlabs
    ensure_dir "${PE_K8S_INSTALL_DIR}"
    ensure_dir "${PE_K8S_SYSCONFIG_DIR}"
    ensure_dir /var/log/puppetlabs
}

stage_optional_license() {
    if [ -f "${PE_LICENSE_PATH}" ]; then
        ensure_dir /etc/puppetlabs
        cp -f "${PE_LICENSE_PATH}" /etc/puppetlabs/license.txt
    fi
}

control_plane_namespace() {
    if [ -n "${PE_CONTROL_PLANE_NAMESPACE}" ]; then
        printf '%s\n' "${PE_CONTROL_PLANE_NAMESPACE}"
        return 0
    fi

    if k8s_namespace >/dev/null 2>&1; then
        k8s_namespace
        return 0
    fi

    printf 'default\n'
}

control_plane_certname() {
    if [ -n "${PE_CONTROL_PLANE_CERTNAME}" ]; then
        printf '%s\n' "${PE_CONTROL_PLANE_CERTNAME}"
        return 0
    fi

    if [ -n "${PE_BOOTSTRAP_PUPPET_CERTNAME}" ]; then
        printf '%s\n' "${PE_BOOTSTRAP_PUPPET_CERTNAME}"
        return 0
    fi

    [ -n "${PE_CONTROL_PLANE_HEADLESS_SERVICE}" ] || {
        log "PE_CONTROL_PLANE_HEADLESS_SERVICE is required when PE_CONTROL_PLANE_CERTNAME is not set"
        exit 1
    }

    printf '%s.%s.%s.svc.cluster.local\n' \
        "${PE_CONTROL_PLANE_POD_NAME}" \
        "${PE_CONTROL_PLANE_HEADLESS_SERVICE}" \
        "$(control_plane_namespace)"
}

control_plane_puppet_master_host() {
    if [ -n "${PE_CONTROL_PLANE_PUPPET_MASTER_HOST}" ]; then
        printf '%s\n' "${PE_CONTROL_PLANE_PUPPET_MASTER_HOST}"
        return 0
    fi

    if [ -n "${PE_BOOTSTRAP_PUPPET_SERVER}" ]; then
        printf '%s\n' "${PE_BOOTSTRAP_PUPPET_SERVER}"
        return 0
    fi

    control_plane_certname
}

control_plane_certificate_authority_host() {
    if [ -n "${PE_BOOTSTRAP_PUPPET_SERVER}" ]; then
        printf '%s\n' "${PE_BOOTSTRAP_PUPPET_SERVER}"
        return 0
    fi

    control_plane_puppet_master_host
}

control_plane_dns_alt_names_hocon() {
    python3 - \
        "$(control_plane_certname)" \
        "${PE_CONTROL_PLANE_POD_NAME}" \
        "${PE_CONTROL_PLANE_HEADLESS_SERVICE}" \
        "$(control_plane_namespace)" \
        "$(control_plane_puppet_master_host)" \
        "${PE_CONTROL_PLANE_SHARED_DNS_ALT_NAMES}" <<'PY'
import json
import sys

certname, pod_name, headless_service, namespace, puppet_master_host, shared_csv = sys.argv[1:7]

items = []

def append(value):
    value = (value or "").strip()
    if value and value not in items:
        items.append(value)

append(certname)
append(puppet_master_host)
append(pod_name)

if pod_name and headless_service:
    append(f"{pod_name}.{headless_service}")
    append(f"{pod_name}.{headless_service}.{namespace}")
    append(f"{pod_name}.{headless_service}.{namespace}.svc")
    append(f"{pod_name}.{headless_service}.{namespace}.svc.cluster.local")

for entry in shared_csv.split(","):
    append(entry)

print(json.dumps(items))
PY
}

render_pe_conf() {
    local rendered_ca_host rendered_certname rendered_puppet_master_host rendered_dns_alt_names_hocon

    require_file "${PE_CONF_TEMPLATE_PATH}"
    rendered_ca_host="$(control_plane_certificate_authority_host)"
    rendered_certname="$(control_plane_certname)"
    rendered_puppet_master_host="$(control_plane_puppet_master_host)"
    rendered_dns_alt_names_hocon="$(control_plane_dns_alt_names_hocon)"

    ensure_dir "$(dirname "${PE_CONF_PATH}")"
    python3 - \
        "${PE_CONF_TEMPLATE_PATH}" \
        "${PE_CONF_PATH}" \
        "${rendered_ca_host}" \
        "${rendered_certname}" \
        "${rendered_puppet_master_host}" \
        "${rendered_dns_alt_names_hocon}" <<'PY'
from pathlib import Path
import sys

template_path, output_path, ca_host, certname, puppet_master_host, dns_alt_names_hocon = sys.argv[1:7]
rendered = Path(template_path).read_text(encoding="utf-8")
rendered = rendered.replace("__PE_CERTIFICATE_AUTHORITY_HOST__", ca_host)
rendered = rendered.replace("__PE_CERTNAME__", certname)
rendered = rendered.replace("__PE_PUPPET_MASTER_HOST__", puppet_master_host)
rendered = rendered.replace("__PE_DNS_ALT_NAMES_HOCON__", dns_alt_names_hocon)
Path(output_path).write_text(rendered, encoding="utf-8")
PY
    log "Rendered pe.conf to ${PE_CONF_PATH}"
}

prepare_control_plane_identity() {
    local certname fqdn domain

    certname="$(control_plane_certname)"
    export PE_BOOTSTRAP_PUPPET_CERTNAME="${PE_BOOTSTRAP_PUPPET_CERTNAME:-${certname}}"
    export PE_BOOTSTRAP_PUPPET_SERVER="${PE_BOOTSTRAP_PUPPET_SERVER:-$(control_plane_puppet_master_host)}"
    export FACTER_hostname="${FACTER_hostname:-${PE_CONTROL_PLANE_POD_NAME}}"
    export FACTER_fqdn="${FACTER_fqdn:-${certname}}"

    fqdn="${FACTER_fqdn}"
    domain="${fqdn#*.}"
    if [ "${domain}" = "${fqdn}" ]; then
        domain=""
    fi
    export FACTER_domain="${FACTER_domain:-${domain}}"
}

seed_bootstrap_puppet_conf() {
    local certname="${PE_BOOTSTRAP_PUPPET_CERTNAME}"
    local server="${PE_BOOTSTRAP_PUPPET_SERVER}"

    if [ -z "${certname}" ] && [ -z "${server}" ]; then
        return
    fi

    ensure_dir /etc/puppetlabs/puppet

    cat > /etc/puppetlabs/puppet/puppet.conf <<EOF
[main]
certname = ${certname}
server = ${server}
user = pe-puppet
group = pe-puppet

[agent]
graph = true
EOF
}

write_summary() {
    cat > "${PE_K8S_EXPORT_SUMMARY}" <<EOF
pe_version=${PE_VERSION:-unknown}
installer_root=${PE_INSTALLER_ROOT}
pe_conf_path=${PE_CONF_PATH}
install_marker=${PE_K8S_INSTALL_MARKER}
exported_sysconfig_dir=${PE_K8S_SYSCONFIG_DIR}
timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
}

run_installer_prep() {
    require_file "${PE_INSTALLER_CMD}"

    log "Running PE installer prep from ${PE_INSTALLER_ROOT} with pe.conf ${PE_CONF_PATH}"
    (
        cd "${PE_INSTALLER_ROOT}"
        "${PE_INSTALLER_CMD}" -c "${PE_CONF_PATH}" -y -p
    )
}

run_install_sequence() {
    local result=0
    local wrapper_pid=""
    local install_version="${PE_VERSION}"
    local cmd=(
        /opt/puppetlabs/puppet/bin/puppet infrastructure configure
        --detailed-exitcodes
        --environmentpath /opt/puppetlabs/server/data/environments
        --environment enterprise
        --no-noop
        --libdir /dev/null
        --factpath /dev/null
        "--install=${install_version}"
        --disable_warnings deprecations
        --install-method=-c_pe_conf
    )

    log "Maintaining service-control wrappers throughout PE install"
    maintain_service_control_wrappers &
    wrapper_pid=$!
    trap 'if [ -n "${wrapper_pid:-}" ]; then kill "${wrapper_pid}" 2>/dev/null || true; wait "${wrapper_pid}" 2>/dev/null || true; fi' RETURN

    run_installer_prep
    install_service_control_wrappers

    log "Running puppet infrastructure configure for PE ${install_version}"
    FACTER_pe_status_check_role=unknown "${cmd[@]}" || result=$?

    case "${result}" in
        0|2)
            return 0
            ;;
        *)
            return "${result}"
            ;;
    esac
}

main() {
    ensure_runtime_mounts
    start_install_logging
    stage_optional_license
    prepare_control_plane_identity
    render_pe_conf
    seed_bootstrap_puppet_conf
    patch_nginx_ingress_redirects

    if [ -f "${PE_K8S_INSTALL_MARKER}" ] && [ "${FORCE_REINSTALL}" != "true" ]; then
        log "PE install marker already exists; skipping install"
        exit 0
    fi

    install_service_control_wrappers
    run_install_sequence
    sync_puppetdb_integration_settings
    export_runtime_rootfs_artifacts
    ensure_pe_build_metadata
    patch_nginx_ingress_redirects
    write_summary
    touch "${PE_K8S_INSTALL_MARKER}"
    /opt/puppetlabs/puppet/bin/puppet agent --enable || true
    log "PE install completed"
}

main "$@"
