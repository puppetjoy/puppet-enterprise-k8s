#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_INSTALLER_ROOT="${PE_INSTALLER_ROOT:-/opt/pe-installer}"
PE_COMPILER_REQUIRED_INSTALL_JOB="${PE_COMPILER_REQUIRED_INSTALL_JOB:-}"
PE_COMPILER_PACKAGE_REPO_NAME="${PE_COMPILER_PACKAGE_REPO_NAME:-pe-k8s-local}"
PE_COMPILER_PACKAGE_REPO_PATH="${PE_COMPILER_PACKAGE_REPO_PATH:-${PE_INSTALLER_ROOT}/packages/el-9-x86_64}"
PE_COMPILER_PACKAGE_NAMES="${PE_COMPILER_PACKAGE_NAMES:-puppet-agent pe-puppet-enterprise-release pe-puppetserver pe-puppetdb pe-puppetdb-termini pe-modules pe-postgresql-common pe-postgresql14 pe-postgresql14-server pe-postgresql14-contrib pe-postgresql14-pglogical pe-postgresql14-pgrepack}"
PE_COMPILER_FORCE_REINSTALL="${PE_COMPILER_FORCE_REINSTALL:-false}"
PE_K8S_COMPILER_INSTALL_DIR="${PE_K8S_COMPILER_INSTALL_DIR:-${PE_K8S_STATE_DIR}/compiler-install}"
PE_K8S_COMPILER_INSTALL_MARKER="${PE_K8S_COMPILER_INSTALL_MARKER:-${PE_K8S_COMPILER_INSTALL_DIR}/install-complete}"
PE_K8S_COMPILER_INSTALL_LOG="${PE_K8S_COMPILER_INSTALL_LOG:-${PE_K8S_COMPILER_INSTALL_DIR}/install.log}"
PE_K8S_COMPILER_REPO_FILE="${PE_K8S_COMPILER_REPO_FILE:-/etc/yum.repos.d/pe-k8s-local.repo}"

start_install_logging() {
    ensure_dir "$(dirname "${PE_K8S_COMPILER_INSTALL_LOG}")"
    touch "${PE_K8S_COMPILER_INSTALL_LOG}"
    exec > >(tee -a "${PE_K8S_COMPILER_INSTALL_LOG}") 2>&1
}

ensure_compiler_runtime_mounts() {
    ensure_dir /etc/puppetlabs
    ensure_dir /opt/puppetlabs
    ensure_dir "${PE_K8S_COMPILER_INSTALL_DIR}"
    ensure_dir "${PE_K8S_SYSCONFIG_DIR}"
    ensure_dir /var/log/puppetlabs
    ensure_dir /etc/yum.repos.d
}

compiler_runtime_ready() {
    local path

    [ -f "${PE_K8S_COMPILER_INSTALL_MARKER}" ] || return 1

    for path in \
        /opt/puppetlabs/bin/puppet \
        /opt/puppetlabs/server/apps/postgresql/14/bin/postgres \
        /opt/puppetlabs/server/apps/puppetserver/bin/puppetserver \
        /opt/puppetlabs/server/apps/puppetdb/bin/puppetdb \
        /opt/puppetlabs/puppet/modules/puppet_enterprise/manifests/init.pp \
        "${PE_K8S_SYSCONFIG_DIR}/pe-puppetserver" \
        "${PE_K8S_SYSCONFIG_DIR}/pe-puppetdb" \
        /opt/puppetlabs/server/pe_build
    do
        [ -e "${path}" ] || return 1
    done

    return 0
}

write_local_repo_file() {
    require_file "${PE_COMPILER_PACKAGE_REPO_PATH}/repodata/repomd.xml"

    cat > "${PE_K8S_COMPILER_REPO_FILE}" <<EOF
[${PE_COMPILER_PACKAGE_REPO_NAME}]
name=PE bundled compiler packages
baseurl=file://${PE_COMPILER_PACKAGE_REPO_PATH}
enabled=1
gpgcheck=0
metadata_expire=0
EOF
}

install_compiler_packages() {
    local wrapper_pid=""
    local packages=()

    read -r -a packages <<< "${PE_COMPILER_PACKAGE_NAMES}"

    [ "${#packages[@]}" -gt 0 ] || {
        log "No compiler package names were provided"
        return 1
    }

    write_local_repo_file
    install_service_control_wrappers
    maintain_service_control_wrappers &
    wrapper_pid=$!
    trap 'if [ -n "${wrapper_pid:-}" ]; then kill "${wrapper_pid}" 2>/dev/null || true; wait "${wrapper_pid}" 2>/dev/null || true; fi' RETURN

    dnf install -y \
        --disablerepo='*' \
        --enablerepo="${PE_COMPILER_PACKAGE_REPO_NAME}" \
        --setopt=install_weak_deps=False \
        "${packages[@]}"
}

main() {
    ensure_compiler_runtime_mounts
    start_install_logging

    if [ -n "${PE_COMPILER_REQUIRED_INSTALL_JOB}" ]; then
        wait_for_k8s_job_completion "${PE_COMPILER_REQUIRED_INSTALL_JOB}"
    fi

    if compiler_runtime_ready && [ "${PE_COMPILER_FORCE_REINSTALL}" != "true" ]; then
        log "Compiler runtime marker already exists; skipping compiler package install"
        exit 0
    fi

    if [ -f "${PE_K8S_COMPILER_INSTALL_MARKER}" ]; then
        log "Compiler runtime marker exists but runtime is incomplete; reinstalling"
        rm -f "${PE_K8S_COMPILER_INSTALL_MARKER}"
    fi

    install_compiler_packages
    ensure_pe_build_metadata
    export_runtime_rootfs_artifacts
    touch "${PE_K8S_COMPILER_INSTALL_MARKER}"
    log "Compiler runtime package install completed"
}

main "$@"
