#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_CONF_PATH="${PE_CONF_PATH:-/config/pe.conf}"
PE_CERT_REGEN_CERTNAME="${PE_CERT_REGEN_CERTNAME:-}"
PE_CERT_REGEN_DNS_ALT_NAMES="${PE_CERT_REGEN_DNS_ALT_NAMES:-}"
PE_CERT_REGEN_MANAGE_PXP="${PE_CERT_REGEN_MANAGE_PXP:-false}"
PE_CERT_REGEN_FORCE="${PE_CERT_REGEN_FORCE:-false}"
PE_ENTERPRISE_CONF_DIR="${PE_ENTERPRISE_CONF_DIR:-/etc/puppetlabs/enterprise/conf.d}"
PE_ENTERPRISE_CONF_PATH="${PE_ENTERPRISE_CONF_PATH:-${PE_ENTERPRISE_CONF_DIR}/pe.conf}"

main() {
    local cert_path
    local result=0
    local cmd

    [ -n "${PE_CERT_REGEN_CERTNAME}" ] || {
        log "PE_CERT_REGEN_CERTNAME is required"
        exit 1
    }

    [ -n "${PE_CERT_REGEN_DNS_ALT_NAMES}" ] || {
        log "PE_CERT_REGEN_DNS_ALT_NAMES is required"
        exit 1
    }

    wait_for_install_marker
    copy_exported_sysconfig_into_rootfs
    require_file "${PE_CONF_PATH}"

    cert_path="/etc/puppetlabs/puppet/ssl/certs/${PE_CERT_REGEN_CERTNAME}.pem"
    if [ "${PE_CERT_REGEN_FORCE}" != "true" ] && cert_matches_desired_dns_alt_names "${cert_path}" "${PE_CERT_REGEN_DNS_ALT_NAMES}"; then
        log "Current certificate for ${PE_CERT_REGEN_CERTNAME} already matches desired SANs; skipping regeneration"
        exit 0
    fi

    ensure_dir "${PE_ENTERPRISE_CONF_DIR}"
    cp -f "${PE_CONF_PATH}" "${PE_ENTERPRISE_CONF_PATH}"

    ensure_dir /var/log/puppetlabs/installer
    chmod 0777 /var/log/puppetlabs/installer || true

    cmd=(
        /opt/puppetlabs/bin/puppet infrastructure run regenerate_primary_certificate
        primary=localhost
        "dns_alt_names=${PE_CERT_REGEN_DNS_ALT_NAMES}"
        "manage_pxp_service=${PE_CERT_REGEN_MANAGE_PXP}"
    )

    if [ "${PE_CERT_REGEN_FORCE}" = "true" ]; then
        cmd+=(--force)
    fi

    log "Running PE certificate regeneration for ${PE_CERT_REGEN_CERTNAME}"
    "${cmd[@]}" || result=$?

    case "${result}" in
        0)
            ;;
        *)
            if cert_matches_desired_dns_alt_names "${cert_path}" "${PE_CERT_REGEN_DNS_ALT_NAMES}"; then
                repair_pe_service_ssl_material "${PE_CERT_REGEN_CERTNAME}"
                log "Certificate regeneration exited ${result}, but the host certificate matches the desired SANs and service SSL copies were repaired"
                exit 0
            fi
            log "Certificate regeneration failed with exit code ${result}"
            log "If the release is left half-rotated, scale pe, pe-puppetserver, and pe-puppetdb to 0 and run the cert recovery job"
            exit "${result}"
            ;;
    esac

    repair_pe_service_ssl_material "${PE_CERT_REGEN_CERTNAME}"

    if cert_matches_desired_dns_alt_names "${cert_path}" "${PE_CERT_REGEN_DNS_ALT_NAMES}"; then
        log "Certificate regeneration completed with desired SANs"
        exit 0
    fi

    log "Certificate regeneration completed but SANs do not match the desired set"
    exit 1
}

main "$@"
