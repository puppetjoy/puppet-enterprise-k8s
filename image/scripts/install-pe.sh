#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_INSTALLER_ROOT="${PE_INSTALLER_ROOT:-/opt/pe-installer}"
PE_CONF_PATH="${PE_CONF_PATH:-/config/pe.conf}"
PE_LICENSE_PATH="${PE_LICENSE_PATH:-/license/license.txt}"
PE_INSTALLER_CMD="${PE_INSTALLER_CMD:-${PE_INSTALLER_ROOT}/puppet-enterprise-installer}"
PE_K8S_EXPORT_SUMMARY="${PE_K8S_EXPORT_SUMMARY:-${PE_K8S_INSTALL_DIR}/install-summary.txt}"
PE_K8S_INSTALL_LOG="${PE_K8S_INSTALL_LOG:-${PE_K8S_INSTALL_DIR}/installer-run.log}"
FORCE_REINSTALL="${FORCE_REINSTALL:-false}"
PE_BOOTSTRAP_PUPPET_CERTNAME="${PE_BOOTSTRAP_PUPPET_CERTNAME:-}"
PE_BOOTSTRAP_PUPPET_SERVER="${PE_BOOTSTRAP_PUPPET_SERVER:-}"

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
    require_file "${PE_CONF_PATH}"
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
    seed_bootstrap_puppet_conf
    patch_nginx_ingress_redirects

    if [ -f "${PE_K8S_INSTALL_MARKER}" ] && [ "${FORCE_REINSTALL}" != "true" ]; then
        log "PE install marker already exists; skipping install"
        exit 0
    fi

    install_service_control_wrappers
    run_install_sequence
    sync_autosign_settings
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
