#!/bin/bash
set -euo pipefail

PE_K8S_STATE_DIR="${PE_K8S_STATE_DIR:-/var/lib/pe-k8s}"
PE_K8S_INSTALL_DIR="${PE_K8S_INSTALL_DIR:-${PE_K8S_STATE_DIR}/install}"
PE_K8S_INSTALL_MARKER="${PE_K8S_INSTALL_MARKER:-${PE_K8S_INSTALL_DIR}/install-complete}"
PE_K8S_SYSCONFIG_DIR="${PE_K8S_SYSCONFIG_DIR:-${PE_K8S_STATE_DIR}/sysconfig}"
PE_K8S_WAIT_TIMEOUT_SECONDS="${PE_K8S_WAIT_TIMEOUT_SECONDS:-3600}"
PE_K8S_AUTOSIGN_MODE="${PE_K8S_AUTOSIGN_MODE:-off}"
PE_K8S_PCP_CONTROLLER_LOCAL_HOST="${PE_K8S_PCP_CONTROLLER_LOCAL_HOST:-puppet}"

log() {
    printf '[pe-k8s] %s\n' "$*"
}

ensure_dir() {
    mkdir -p "$1"
}

require_file() {
    local path="$1"
    [ -f "${path}" ] || {
        log "Required file is missing: ${path}"
        return 1
    }
}

sync_autosign_settings() {
    local puppet_bin=/opt/puppetlabs/bin/puppet

    case "${PE_K8S_AUTOSIGN_MODE}" in
        ""|off|false)
            if [ -x "${puppet_bin}" ] && [ -f /etc/puppetlabs/puppet/puppet.conf ]; then
                "${puppet_bin}" config delete autosign --section main >/dev/null 2>&1 || true
            fi
            rm -f /etc/puppetlabs/puppet/autosign.conf
            ;;
        naive|true)
            ensure_dir /etc/puppetlabs/puppet
            if [ -x "${puppet_bin}" ]; then
                "${puppet_bin}" config set autosign true --section main
            else
                log "Puppet binary not available yet; skipping autosign sync"
            fi
            rm -f /etc/puppetlabs/puppet/autosign.conf
            ;;
        *)
            log "Unsupported autosign mode: ${PE_K8S_AUTOSIGN_MODE}"
            return 1
            ;;
    esac
}

sync_puppetdb_integration_settings() {
    local puppet_bin=/opt/puppetlabs/bin/puppet

    if [ ! -x "${puppet_bin}" ] || [ ! -f /etc/puppetlabs/puppet/puppet.conf ]; then
        log "Puppet binary or puppet.conf missing; skipping PuppetDB integration sync"
        return 0
    fi

    "${puppet_bin}" config set storeconfigs true --section master
    "${puppet_bin}" config set storeconfigs_backend puppetdb --section master
    "${puppet_bin}" config set reports puppetdb,store --section master
}

copy_exported_sysconfig_into_rootfs() {
    ensure_dir /etc/sysconfig
    if [ -d "${PE_K8S_SYSCONFIG_DIR}" ]; then
        find "${PE_K8S_SYSCONFIG_DIR}" -maxdepth 1 -type f -name 'pe-*' -print0 | while IFS= read -r -d '' path; do
            cp -f "${path}" /etc/sysconfig/
        done
    fi
}

patch_local_pcp_controller_uri() {
    local config_path=/etc/puppetlabs/orchestration-services/conf.d/pcp-broker.conf
    local desired_uri
    local escaped_host

    [ -n "${PE_K8S_PCP_CONTROLLER_LOCAL_HOST}" ] || return 0
    [ -f "${config_path}" ] || return 0

    desired_uri="wss://${PE_K8S_PCP_CONTROLLER_LOCAL_HOST}:8143/server"
    if grep -Fq "\"${desired_uri}\"" "${config_path}"; then
        return 0
    fi

    escaped_host="$(printf '%s' "${PE_K8S_PCP_CONTROLLER_LOCAL_HOST}" | sed 's/[\/&]/\\&/g')"
    sed -i -E "s#wss://[^/\"]+:8143/server#wss://${escaped_host}:8143/server#g" "${config_path}"
    log "Patched PCP controller URI to ${desired_uri}"
}

normalize_dns_alt_names() {
    tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | sed '/^$/d' | sort -u
}

current_dns_alt_names() {
    local cert_path="$1"

    openssl x509 -in "${cert_path}" -noout -ext subjectAltName 2>/dev/null | \
        tr ',' '\n' | sed -n 's/^[[:space:]]*DNS://p' | sed 's/[[:space:]]*$//' | sed '/^$/d' | sort -u
}

desired_dns_alt_names() {
    local dns_alt_names="$1"

    printf '%s\n' "${dns_alt_names}" | normalize_dns_alt_names
}

cert_matches_desired_dns_alt_names() {
    local cert_path="$1"
    local dns_alt_names="$2"
    local current desired

    [ -f "${cert_path}" ] || return 1

    current="$(current_dns_alt_names "${cert_path}")"
    desired="$(desired_dns_alt_names "${dns_alt_names}")"
    [ "${current}" = "${desired}" ]
}

remove_pe_host_identity_material() {
    local certname="$1"

    rm -f \
        "/etc/puppetlabs/puppet/ssl/certs/${certname}.pem" \
        "/etc/puppetlabs/puppet/ssl/private_keys/${certname}.pem" \
        "/etc/puppetlabs/puppet/ssl/public_keys/${certname}.pem" \
        "/etc/puppetlabs/puppet/ssl/certificate_requests/${certname}.pem" \
        "/etc/puppetlabs/puppetserver/ca/signed/${certname}.pem" \
        "/etc/puppetlabs/puppetserver/ca/requests/${certname}.pem"
}

latest_backup_dir() {
    local base_dir="$1"

    ls -dt "${base_dir}"_bak_* 2>/dev/null | head -n 1 || true
}

pe_service_ssl_targets() {
    cat <<'EOF'
/etc/puppetlabs/puppetdb/ssl:/etc/puppetlabs/puppetdb/ssl
/etc/puppetlabs/orchestration-services/ssl:/etc/puppetlabs/orchestration-services/ssl
/opt/puppetlabs/server/data/console-services/certs:/opt/puppetlabs/server/data/console-services/certs
/opt/puppetlabs/server/data/host-action-collector/ssl:/opt/puppetlabs/server/data/host-action-collector/ssl
/opt/puppetlabs/server/data/infra-assistant/ssl:/opt/puppetlabs/server/data/infra-assistant/ssl
/opt/puppetlabs/server/data/patching-service/ssl:/opt/puppetlabs/server/data/patching-service/ssl
/opt/puppetlabs/server/data/workflow-service/ssl:/opt/puppetlabs/server/data/workflow-service/ssl
/etc/puppetlabs/bolt-server/ssl:
/etc/puppetlabs/ace-server/ssl:
EOF
}

sync_pe_service_ssl_material() {
    local cert_path="$1"
    local key_path="$2"
    local pk8_path
    local dir
    local backup_base
    local backup_dir

    require_file "${cert_path}"
    require_file "${key_path}"

    pk8_path="$(mktemp)"
    openssl pkcs8 -topk8 -inform PEM -outform DER -nocrypt -in "${key_path}" -out "${pk8_path}"

    while IFS=: read -r dir backup_base; do
        [ -n "${dir}" ] || continue

        ensure_dir "${dir}"

        cp -f "${cert_path}" "${dir}/pe.cert.pem"
        cp -f "${key_path}" "${dir}/pe.private_key.pem"
        cp -f "${pk8_path}" "${dir}/pe.private_key.pk8"

        backup_dir=""
        if [ -n "${backup_base}" ]; then
            backup_dir="$(latest_backup_dir "${backup_base}")"
        fi

        if [ -n "${backup_dir}" ] && [ -f "${backup_dir}/pe.cert.pem" ]; then
            chown --reference="${backup_dir}/pe.cert.pem" "${dir}/pe.cert.pem"
            chmod --reference="${backup_dir}/pe.cert.pem" "${dir}/pe.cert.pem"
        else
            chown --reference="${dir}" "${dir}/pe.cert.pem"
            chmod 0444 "${dir}/pe.cert.pem"
        fi

        if [ -n "${backup_dir}" ] && [ -f "${backup_dir}/pe.private_key.pem" ]; then
            chown --reference="${backup_dir}/pe.private_key.pem" "${dir}/pe.private_key.pem"
            chmod --reference="${backup_dir}/pe.private_key.pem" "${dir}/pe.private_key.pem"
        else
            chown --reference="${dir}" "${dir}/pe.private_key.pem"
            chmod 0400 "${dir}/pe.private_key.pem"
        fi

        if [ -n "${backup_dir}" ] && [ -f "${backup_dir}/pe.private_key.pk8" ]; then
            chown --reference="${backup_dir}/pe.private_key.pk8" "${dir}/pe.private_key.pk8"
            chmod --reference="${backup_dir}/pe.private_key.pk8" "${dir}/pe.private_key.pk8"
        else
            chown --reference="${dir}" "${dir}/pe.private_key.pk8"
            chmod 0400 "${dir}/pe.private_key.pk8"
        fi
    done < <(pe_service_ssl_targets)

    rm -f "${pk8_path}"
}

repair_pe_service_ssl_material() {
    local certname="$1"

    sync_pe_service_ssl_material \
        "/etc/puppetlabs/puppet/ssl/certs/${certname}.pem" \
        "/etc/puppetlabs/puppet/ssl/private_keys/${certname}.pem"
}

patch_nginx_ingress_redirects() {
    local path

    path=/etc/puppetlabs/nginx/conf.d/proxy.conf
    if [ -f "${path}" ]; then
        sed -i 's/ ipv6only=off//g' "${path}"
    fi

    path=/etc/puppetlabs/nginx/conf.d/http_redirect.conf
    if [ -f "${path}" ]; then
        cat > "${path}" <<'EOF'
map $http_x_forwarded_proto $pe_ingress_https {
  default 0;
  https 1;
}

server {
  server_name _;
  listen 0.0.0.0:80;

  if ($pe_ingress_https != 1) {
    return 301 https://$http_host$request_uri;
  }

  location / {
    proxy_pass http://localhost:4430;
    proxy_redirect http://localhost:4430 /;
    proxy_read_timeout 120;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
  }

  location /saml {
    proxy_pass https://0.0.0.0:4431;
    proxy_redirect https://0.0.0.0:4431 /;
    proxy_read_timeout 120;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
  }
}
EOF
    fi
}

user_home() {
    local user="$1"
    getent passwd "${user}" | cut -d: -f6
}

prepare_user_env() {
    local user="$1"
    local home

    home="$(user_home "${user}")"
    [ -n "${home}" ] || home="/"

    export HOME="${home}"
    export USER="${user}"
    export LOGNAME="${user}"
}

wait_for_install_marker() {
    local elapsed=0
    local interval=5

    while [ ! -f "${PE_K8S_INSTALL_MARKER}" ]; do
        if [ "${elapsed}" -ge "${PE_K8S_WAIT_TIMEOUT_SECONDS}" ]; then
            log "Timed out waiting for install marker ${PE_K8S_INSTALL_MARKER}"
            return 1
        fi
        sleep "${interval}"
        elapsed=$((elapsed + interval))
    done
}

exec_as_user() {
    local user="$1"
    shift

    if [ "$(id -u)" -eq 0 ]; then
        prepare_user_env "${user}"
        exec runuser --preserve-environment -u "${user}" -- "$@"
    fi

    exec "$@"
}
