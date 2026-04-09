#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

PE_SIGN_CERTNAMES="${PE_SIGN_CERTNAMES:-${PE_COMPILER_CERTNAMES:-}}"
PE_SIGN_CERT_WAIT_TIMEOUT_SECONDS="${PE_SIGN_CERT_WAIT_TIMEOUT_SECONDS:-900}"
PE_SIGN_CERT_LABEL="${PE_SIGN_CERT_LABEL:-certificate}"
PE_SIGN_PE_SERVICE="${PE_SIGN_PE_SERVICE:-pe}"
PE_SIGN_LOGIN="${PE_SIGN_LOGIN:-admin}"
PE_SIGN_PASSWORD="${PE_SIGN_PASSWORD:-}"
PE_SIGN_PE_CONF_PATH="${PE_SIGN_PE_CONF_PATH:-/config/pe.conf}"

cert_status_response_path() {
    local certname="$1"
    printf '/tmp/pe-k8s-cert-status-%s.json\n' "$(printf '%s' "${certname}" | tr '/:' '__')"
}

sign_payload_path() {
    local certname="$1"
    printf '/tmp/pe-k8s-cert-sign-%s.json\n' "$(printf '%s' "${certname}" | tr '/:' '__')"
}

cert_state() {
    python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

print((data.get("state") or "").strip())
PY
}

signing_password() {
    if [ -n "${PE_SIGN_PASSWORD}" ]; then
        printf '%s\n' "${PE_SIGN_PASSWORD}"
        return 0
    fi

    hocon_string_setting console_admin_password "${PE_SIGN_PE_CONF_PATH}"
}

fetch_cert_status() {
    local certname="$1"
    local token="$2"
    local response_path

    response_path="$(cert_status_response_path "${certname}")"
    curl -sk \
        -o "${response_path}" \
        -w '%{http_code}' \
        -H "X-Authentication: ${token}" \
        "https://${PE_SIGN_PE_SERVICE}:8140/puppet-ca/v1/certificate_status/${certname}"
}

sign_pending_request() {
    local certname="$1"
    local token="$2"
    local http_code response_path state sign_path sign_http_code

    response_path="$(cert_status_response_path "${certname}")"
    http_code="$(fetch_cert_status "${certname}" "${token}")"

    case "${http_code}" in
        200)
            state="$(cert_state "${response_path}")"
            case "${state}" in
                signed)
                    return 0
                    ;;
                requested)
                    log "Signing ${PE_SIGN_CERT_LABEL} ${certname}"
                    sign_path="$(sign_payload_path "${certname}")"
                    sign_http_code="$(curl -sk \
                        -o "${sign_path}" \
                        -w '%{http_code}' \
                        -X PUT \
                        -H "X-Authentication: ${token}" \
                        -H "Content-Type: application/json" \
                        --data '{"desired_state":"signed"}' \
                        "https://${PE_SIGN_PE_SERVICE}:8140/puppet-ca/v1/certificate_status/${certname}")"
                    case "${sign_http_code}" in
                        200|204)
                            return 0
                            ;;
                        *)
                            log "Signing ${PE_SIGN_CERT_LABEL} ${certname} failed with HTTP ${sign_http_code}"
                            return 1
                            ;;
                    esac
                    ;;
                *)
                    log "Unexpected ${PE_SIGN_CERT_LABEL} state for ${certname}: ${state}"
                    return 1
                    ;;
            esac
            ;;
        404)
            return 1
            ;;
        *)
            log "Failed to query ${PE_SIGN_CERT_LABEL} ${certname}: HTTP ${http_code}"
            return 1
            ;;
    esac
}

all_certs_signed() {
    local token="$1"
    shift
    local certname http_code response_path state

    for certname in "$@"; do
        response_path="$(cert_status_response_path "${certname}")"
        http_code="$(fetch_cert_status "${certname}" "${token}")"
        if [ "${http_code}" != "200" ]; then
            return 1
        fi
        state="$(cert_state "${response_path}")"
        [ "${state}" = "signed" ] || return 1
    done

    return 0
}

main() {
    local certnames deadline certname password token

    [ -n "${PE_SIGN_CERTNAMES}" ] || {
        log "No certnames requested; skipping signer job"
        exit 0
    }

    password="$(signing_password || true)"
    [ -n "${password}" ] || {
        log "Unable to determine console admin password for signer job"
        exit 1
    }

    wait_for_remote_pe_status "${PE_SIGN_PE_SERVICE}" "${PE_SIGN_CERT_WAIT_TIMEOUT_SECONDS}"
    wait_for_remote_console "${PE_SIGN_PE_SERVICE}" "${PE_SIGN_CERT_WAIT_TIMEOUT_SECONDS}"
    token="$(issue_rbac_token "${PE_SIGN_PE_SERVICE}" "${PE_SIGN_LOGIN}" "${password}" "1h" "pe-k8s-${PE_SIGN_CERT_LABEL}")" || {
        log "Failed to generate an RBAC token for signer job"
        exit 1
    }

    IFS=',' read -r -a certnames <<< "${PE_SIGN_CERTNAMES}"
    deadline=$((SECONDS + PE_SIGN_CERT_WAIT_TIMEOUT_SECONDS))

    while [ "${SECONDS}" -lt "${deadline}" ]; do
        for certname in "${certnames[@]}"; do
            [ -n "${certname}" ] || continue
            sign_pending_request "${certname}" "${token}" || true
        done

        if all_certs_signed "${token}" "${certnames[@]}"; then
            log "All requested certificates are signed"
            exit 0
        fi

        sleep 5
    done

    for certname in "${certnames[@]}"; do
        if ! all_certs_signed "${token}" "${certname}"; then
            log "Timed out waiting to sign ${PE_SIGN_CERT_LABEL} ${certname}"
        fi
    done

    exit 1
}

main "$@"
