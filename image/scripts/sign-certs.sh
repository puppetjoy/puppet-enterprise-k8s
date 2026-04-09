#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_SIGN_CERTNAMES="${PE_SIGN_CERTNAMES:-${PE_COMPILER_CERTNAMES:-}}"
PE_SIGN_CERT_WAIT_TIMEOUT_SECONDS="${PE_SIGN_CERT_WAIT_TIMEOUT_SECONDS:-900}"
PE_SIGN_REQUIRED_INSTALL_JOB="${PE_SIGN_REQUIRED_INSTALL_JOB:-${PE_COMPILER_REQUIRED_INSTALL_JOB:-}}"
PE_SIGN_CERT_LABEL="${PE_SIGN_CERT_LABEL:-certificate}"

cert_path_for() {
    printf '/etc/puppetlabs/puppetserver/ca/signed/%s.pem\n' "$1"
}

request_path_for() {
    printf '/etc/puppetlabs/puppetserver/ca/requests/%s.pem\n' "$1"
}

cert_is_signed() {
    [ -f "$(cert_path_for "$1")" ]
}

cert_request_pending() {
    [ -f "$(request_path_for "$1")" ]
}

sign_pending_request() {
    local certname="$1"

    if cert_is_signed "${certname}"; then
        return 0
    fi

    if cert_request_pending "${certname}"; then
        log "Signing ${PE_SIGN_CERT_LABEL} ${certname}"
        /opt/puppetlabs/server/bin/puppetserver ca sign --certname "${certname}"
    fi

    return 0
}

all_certs_signed() {
    local certname

    for certname in "$@"; do
        cert_is_signed "${certname}" || return 1
    done
    return 0
}

main() {
    local certnames deadline certname

    if [ -n "${PE_SIGN_REQUIRED_INSTALL_JOB}" ]; then
        wait_for_k8s_job_completion "${PE_SIGN_REQUIRED_INSTALL_JOB}"
    else
        wait_for_install_marker
    fi
    copy_exported_sysconfig_into_rootfs

    [ -n "${PE_SIGN_CERTNAMES}" ] || {
        log "No certnames requested; skipping signer job"
        exit 0
    }

    IFS=',' read -r -a certnames <<< "${PE_SIGN_CERTNAMES}"
    deadline=$((SECONDS + PE_SIGN_CERT_WAIT_TIMEOUT_SECONDS))

    while [ "${SECONDS}" -lt "${deadline}" ]; do
        for certname in "${certnames[@]}"; do
            [ -n "${certname}" ] || continue
            sign_pending_request "${certname}"
        done

        if all_certs_signed "${certnames[@]}"; then
            log "All requested certificates are signed"
            exit 0
        fi

        sleep 5
    done

    for certname in "${certnames[@]}"; do
        if ! cert_is_signed "${certname}"; then
            log "Timed out waiting to sign ${PE_SIGN_CERT_LABEL} ${certname}"
        fi
    done

    exit 1
}

main "$@"
