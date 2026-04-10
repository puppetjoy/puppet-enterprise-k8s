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
PE_CONTROL_PLANE_CA_PROVIDER="${PE_CONTROL_PLANE_CA_PROVIDER:-selfSigned}"
PE_CONTROL_PLANE_CA_IMPORT_TIMEOUT_SECONDS="${PE_CONTROL_PLANE_CA_IMPORT_TIMEOUT_SECONDS:-300}"
PE_CONTROL_PLANE_CA_ROOT_SECRET_NAMESPACE="${PE_CONTROL_PLANE_CA_ROOT_SECRET_NAMESPACE:-}"
PE_CONTROL_PLANE_CA_ROOT_SECRET_NAME="${PE_CONTROL_PLANE_CA_ROOT_SECRET_NAME:-}"
PE_CONTROL_PLANE_CA_BUNDLE_SECRET_NAME="${PE_CONTROL_PLANE_CA_BUNDLE_SECRET_NAME:-}"

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

control_plane_dns_alt_names_csv() {
    python3 - \
        "$(control_plane_certname)" \
        "${PE_CONTROL_PLANE_POD_NAME}" \
        "${PE_CONTROL_PLANE_HEADLESS_SERVICE}" \
        "$(control_plane_namespace)" \
        "$(control_plane_puppet_master_host)" \
        "${PE_CONTROL_PLANE_SHARED_DNS_ALT_NAMES}" <<'PY'
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

print(",".join(items))
PY
}

control_plane_ca_secret_name() {
    printf '%s-ca\n' "${PE_CONTROL_PLANE_POD_NAME}"
}

control_plane_ca_root_secret_namespace() {
    if [ -n "${PE_CONTROL_PLANE_CA_ROOT_SECRET_NAMESPACE}" ]; then
        printf '%s\n' "${PE_CONTROL_PLANE_CA_ROOT_SECRET_NAMESPACE}"
        return 0
    fi

    control_plane_namespace
}

control_plane_ca_bundle_secret_name() {
    if [ -n "${PE_CONTROL_PLANE_CA_BUNDLE_SECRET_NAME}" ]; then
        printf '%s\n' "${PE_CONTROL_PLANE_CA_BUNDLE_SECRET_NAME}"
        return 0
    fi

    return 1
}

control_plane_ca_uses_cert_manager() {
    [ "${PE_CONTROL_PLANE_CA_PROVIDER}" = "certManager" ]
}

control_plane_ca_uses_seed_secret() {
    [ "${PE_CONTROL_PLANE_CA_PROVIDER}" = "releaseRoot" ]
}

control_plane_ca_imported() {
    [ -f /etc/puppetlabs/puppetserver/ca/ca_crt.pem ] && [ -f /etc/puppetlabs/puppetserver/ca/ca_key.pem ]
}

wait_for_secret_data_field_file() {
    local namespace="$1"
    local secret_name="$2"
    local field_name="$3"
    local output_path="$4"
    local timeout="${5:-${PE_CONTROL_PLANE_CA_IMPORT_TIMEOUT_SECONDS}}"
    local deadline

    ensure_dir "$(dirname "${output_path}")"
    deadline=$((SECONDS + timeout))
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        if k8s_secret_data_field "${namespace}" "${secret_name}" "${field_name}" > "${output_path}"; then
            return 0
        fi
        sleep 5
    done

    log "Timed out waiting for Secret ${namespace}/${secret_name} field ${field_name}"
    return 1
}

wait_for_secret_first_available_field_file() {
    local namespace="$1"
    local secret_name="$2"
    local output_path="$3"
    local timeout="${4:-${PE_CONTROL_PLANE_CA_IMPORT_TIMEOUT_SECONDS}}"
    shift 4

    local field_name
    local deadline

    ensure_dir "$(dirname "${output_path}")"
    deadline=$((SECONDS + timeout))
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        for field_name in "$@"; do
            if k8s_secret_data_field "${namespace}" "${secret_name}" "${field_name}" > "${output_path}"; then
                return 0
            fi
        done
        sleep 5
    done

    log "Timed out waiting for Secret ${namespace}/${secret_name} fields $*"
    return 1
}

generate_empty_ca_crl() {
    local cert_path="$1"
    local key_path="$2"
    local workspace="$3"
    local output_path="$4"

    require_file "${cert_path}"
    require_file "${key_path}"

    ensure_dir "${workspace}/certs"
    : > "${workspace}/index.txt"
    printf '1000\n' > "${workspace}/serial"
    printf '1000\n' > "${workspace}/crlnumber"

    cat > "${workspace}/openssl.cnf" <<EOF
[ ca ]
default_ca = CA_default
[ CA_default ]
dir = ${workspace}
new_certs_dir = \$dir/certs
database = \$dir/index.txt
serial = \$dir/serial
crlnumber = \$dir/crlnumber
default_md = sha256
default_crl_days = 3650
certificate = ${cert_path}
private_key = ${key_path}
policy = policy_loose
copy_extensions = copy
[ policy_loose ]
commonName = supplied
EOF

    openssl ca -config "${workspace}/openssl.cnf" -gencrl -out "${output_path}" >/dev/null 2>&1
}

import_control_plane_ca_from_cert_manager() {
    local control_plane_ca_secret_name
    local root_secret_namespace
    local root_secret_name
    local certname
    local subject_alt_names

    control_plane_ca_uses_cert_manager || return 0

    if control_plane_ca_imported; then
        log "Control-plane CA already present; skipping cert-manager CA import"
        return 0
    fi

    control_plane_ca_secret_name="$(control_plane_ca_secret_name)"
    root_secret_namespace="$(control_plane_ca_root_secret_namespace)"
    root_secret_name="${PE_CONTROL_PLANE_CA_ROOT_SECRET_NAME}"
    certname="$(control_plane_certname)"
    subject_alt_names="$(control_plane_dns_alt_names_csv)"

    [ -n "${root_secret_name}" ] || {
        log "PE_CONTROL_PLANE_CA_ROOT_SECRET_NAME is required for cert-manager CA import"
        return 1
    }

    (
        set -euo pipefail

        local import_dir
        import_dir="$(mktemp -d /tmp/pe-k8s-control-plane-ca.XXXXXX)"
        trap 'rm -rf "${import_dir}"' EXIT

        wait_for_secret_data_field_file \
            "$(control_plane_namespace)" \
            "${control_plane_ca_secret_name}" \
            tls.crt \
            "${import_dir}/intermediate.crt"
        wait_for_secret_data_field_file \
            "$(control_plane_namespace)" \
            "${control_plane_ca_secret_name}" \
            tls.key \
            "${import_dir}/intermediate.key"
        wait_for_secret_first_available_field_file \
            "${root_secret_namespace}" \
            "${root_secret_name}" \
            "${import_dir}/root.crt" \
            "${PE_CONTROL_PLANE_CA_IMPORT_TIMEOUT_SECONDS}" \
            tls.crt ca.crt
        wait_for_secret_data_field_file \
            "${root_secret_namespace}" \
            "${root_secret_name}" \
            tls.key \
            "${import_dir}/root.key"

        cat "${import_dir}/intermediate.crt" "${import_dir}/root.crt" > "${import_dir}/cert-bundle.pem"
        generate_empty_ca_crl \
            "${import_dir}/intermediate.crt" \
            "${import_dir}/intermediate.key" \
            "${import_dir}/intermediate-crl" \
            "${import_dir}/intermediate.crl"
        generate_empty_ca_crl \
            "${import_dir}/root.crt" \
            "${import_dir}/root.key" \
            "${import_dir}/root-crl" \
            "${import_dir}/root.crl"
        cat "${import_dir}/intermediate.crl" "${import_dir}/root.crl" > "${import_dir}/crl-chain.pem"

        log \
            "Importing cert-manager control-plane CA from Secret " \
            "$(control_plane_namespace)/${control_plane_ca_secret_name} " \
            "with root Secret ${root_secret_namespace}/${root_secret_name}"
        if [ -n "${subject_alt_names}" ]; then
            /opt/puppetlabs/bin/puppetserver ca import \
                --private-key "${import_dir}/intermediate.key" \
                --cert-bundle "${import_dir}/cert-bundle.pem" \
                --crl-chain "${import_dir}/crl-chain.pem" \
                --certname "${certname}" \
                --subject-alt-names "${subject_alt_names}"
        else
            /opt/puppetlabs/bin/puppetserver ca import \
                --private-key "${import_dir}/intermediate.key" \
                --cert-bundle "${import_dir}/cert-bundle.pem" \
                --crl-chain "${import_dir}/crl-chain.pem" \
                --certname "${certname}"
        fi
    )
}

import_control_plane_ca_from_seed_secret() {
    local control_plane_ca_secret_name
    local certname
    local subject_alt_names

    control_plane_ca_uses_seed_secret || return 0

    if control_plane_ca_imported; then
        log "Control-plane CA already present; skipping seeded CA import"
        return 0
    fi

    control_plane_ca_secret_name="$(control_plane_ca_secret_name)"
    certname="$(control_plane_certname)"
    subject_alt_names="$(control_plane_dns_alt_names_csv)"

    (
        set -euo pipefail

        local import_dir
        import_dir="$(mktemp -d /tmp/pe-k8s-control-plane-ca.XXXXXX)"
        trap 'rm -rf "${import_dir}"' EXIT

        wait_for_secret_data_field_file \
            "$(control_plane_namespace)" \
            "${control_plane_ca_secret_name}" \
            tls.crt \
            "${import_dir}/intermediate.crt"
        wait_for_secret_data_field_file \
            "$(control_plane_namespace)" \
            "${control_plane_ca_secret_name}" \
            tls.key \
            "${import_dir}/intermediate.key"
        wait_for_secret_first_available_field_file \
            "$(control_plane_namespace)" \
            "${control_plane_ca_secret_name}" \
            "${import_dir}/root.crt" \
            "${PE_CONTROL_PLANE_CA_IMPORT_TIMEOUT_SECONDS}" \
            ca.crt tls.crt
        wait_for_secret_data_field_file \
            "$(control_plane_namespace)" \
            "${control_plane_ca_secret_name}" \
            crl.pem \
            "${import_dir}/crl-chain.pem"

        cat "${import_dir}/intermediate.crt" "${import_dir}/root.crt" > "${import_dir}/cert-bundle.pem"

        log "Importing seeded control-plane CA from Secret $(control_plane_namespace)/${control_plane_ca_secret_name}"
        if [ -n "${subject_alt_names}" ]; then
            /opt/puppetlabs/bin/puppetserver ca import \
                --private-key "${import_dir}/intermediate.key" \
                --cert-bundle "${import_dir}/cert-bundle.pem" \
                --crl-chain "${import_dir}/crl-chain.pem" \
                --certname "${certname}" \
                --subject-alt-names "${subject_alt_names}"
        else
            /opt/puppetlabs/bin/puppetserver ca import \
                --private-key "${import_dir}/intermediate.key" \
                --cert-bundle "${import_dir}/cert-bundle.pem" \
                --crl-chain "${import_dir}/crl-chain.pem" \
                --certname "${certname}"
        fi
    )
}

sync_control_plane_ca_bundle_from_seed_secret() {
    local bundle_secret_name

    control_plane_ca_uses_seed_secret || return 0

    bundle_secret_name="$(control_plane_ca_bundle_secret_name || true)"
    [ -n "${bundle_secret_name}" ] || return 0

    (
        set -euo pipefail

        local bundle_dir
        bundle_dir="$(mktemp -d /tmp/pe-k8s-control-plane-ca-bundle.XXXXXX)"
        trap 'rm -rf "${bundle_dir}"' EXIT

        wait_for_secret_first_available_field_file \
            "$(control_plane_namespace)" \
            "${bundle_secret_name}" \
            "${bundle_dir}/ca.pem" \
            "${PE_CONTROL_PLANE_CA_IMPORT_TIMEOUT_SECONDS}" \
            ca.pem ca.crt
        if k8s_secret_data_field \
            "$(control_plane_namespace)" \
            "${bundle_secret_name}" \
            crl.pem > "${bundle_dir}/crl.pem"
        then
            install -o pe-puppet -g pe-puppet -m 0644 \
                "${bundle_dir}/crl.pem" \
                /etc/puppetlabs/puppet/ssl/crl.pem
            install -o pe-puppet -g pe-puppet -m 0640 \
                "${bundle_dir}/crl.pem" \
                /etc/puppetlabs/puppetserver/ca/ca_crl.pem
            install -o pe-puppet -g pe-puppet -m 0640 \
                "${bundle_dir}/crl.pem" \
                /etc/puppetlabs/puppetserver/ca/infra_crl.pem
        fi

        install -o pe-puppet -g pe-puppet -m 0644 \
            "${bundle_dir}/ca.pem" \
            /etc/puppetlabs/puppet/ssl/certs/ca.pem
        install -o pe-puppet -g pe-puppet -m 0640 \
            "${bundle_dir}/ca.pem" \
            /etc/puppetlabs/puppetserver/ca/ca_crt.pem
        log "Synchronized release-wide control-plane CA bundle from Secret $(control_plane_namespace)/${bundle_secret_name}"
    )
}

import_control_plane_ca() {
    case "${PE_CONTROL_PLANE_CA_PROVIDER}" in
        certManager)
            import_control_plane_ca_from_cert_manager
            ;;
        releaseRoot)
            import_control_plane_ca_from_seed_secret
            ;;
    esac

    sync_control_plane_ca_bundle_from_seed_secret
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
    import_control_plane_ca
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

refresh_existing_install_state() {
    install_service_control_wrappers
    import_control_plane_ca
    sync_puppetdb_integration_settings
    export_runtime_rootfs_artifacts
    ensure_pe_build_metadata
    patch_nginx_ingress_redirects
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
        log "PE install marker already exists; refreshing runtime state"
        refresh_existing_install_state
        log "PE runtime refresh completed"
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
