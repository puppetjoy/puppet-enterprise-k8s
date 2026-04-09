#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_CONF_PATH="${PE_CONF_PATH:-/config/pe.conf}"
PE_CERT_REGEN_CERTNAME="${PE_CERT_REGEN_CERTNAME:-}"
PE_CERT_REGEN_DNS_ALT_NAMES="${PE_CERT_REGEN_DNS_ALT_NAMES:-}"
PE_ENTERPRISE_CONF_DIR="${PE_ENTERPRISE_CONF_DIR:-/etc/puppetlabs/enterprise/conf.d}"
PE_ENTERPRISE_CONF_PATH="${PE_ENTERPRISE_CONF_PATH:-${PE_ENTERPRISE_CONF_DIR}/pe.conf}"

sync_desired_pe_conf() {
    require_file "${PE_CONF_PATH}"
    ensure_dir "${PE_ENTERPRISE_CONF_DIR}"
    cp -f "${PE_CONF_PATH}" "${PE_ENTERPRISE_CONF_PATH}"
}

generate_host_cert_offline() {
    local certname="$1"
    local cert_path="/etc/puppetlabs/puppet/ssl/certs/${certname}.pem"

    if cert_matches_desired_dns_alt_names "${cert_path}" "${PE_CERT_REGEN_DNS_ALT_NAMES}"; then
        log "Host certificate for ${certname} already matches the desired SANs"
        return 0
    fi

    log "Regenerating ${certname} host certificate offline from the on-disk CA"
    remove_pe_host_identity_material "${certname}"

    /opt/puppetlabs/server/bin/puppetserver ca generate \
        --certname "${certname}" \
        --subject-alt-names "${PE_CERT_REGEN_DNS_ALT_NAMES}" \
        --ca-client \
        --force
}

main() {
    local cert_path

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
    sync_desired_pe_conf

    cert_path="/etc/puppetlabs/puppet/ssl/certs/${PE_CERT_REGEN_CERTNAME}.pem"
    generate_host_cert_offline "${PE_CERT_REGEN_CERTNAME}"
    repair_pe_service_ssl_material "${PE_CERT_REGEN_CERTNAME}"

    if cert_matches_desired_dns_alt_names "${cert_path}" "${PE_CERT_REGEN_DNS_ALT_NAMES}"; then
        log "Certificate recovery completed with desired SANs and repaired service SSL copies"
        exit 0
    fi

    log "Certificate recovery completed but SANs do not match the desired set"
    exit 1
}

main "$@"
