#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

role="${1:-${PE_K8S_ROLE:-}}"
[ -n "${role}" ] || exit 1

wait_for_install_marker

curl_with_local_role_cert() {
    local cert_dir="$1"
    shift

    exec curl -skf \
        --cert "${cert_dir}/pe.cert.pem" \
        --key "${cert_dir}/pe.private_key.pem" \
        --cacert /etc/puppetlabs/puppet/ssl/certs/ca.pem \
        "$@"
}

case "${role}" in
    postgresql)
        exec /opt/puppetlabs/server/apps/postgresql/14/bin/pg_isready \
            -h 127.0.0.1 \
            -p "${PGPORT:-5432}"
        ;;
    puppetdb)
        exec curl -skf https://127.0.0.1:8081/status/v1/services/status-service
        ;;
    puppetserver)
        exec curl -skf https://127.0.0.1:8140/status/v1/services
        ;;
    nginx)
        exec curl -skf https://127.0.0.1:443/
        ;;
    console-services)
        exec curl -skf https://127.0.0.1:4433/status/v1/services/status-service
        ;;
    orchestration-services)
        exec curl -skf https://127.0.0.1:8143/status/v1/services/status-service
        ;;
    host-action-collector)
        exec curl -skf https://127.0.0.1:8147/status/v1/services
        ;;
    bolt-server)
        curl_with_local_role_cert /etc/puppetlabs/bolt-server/ssl \
            https://127.0.0.1:62658/admin/status
        ;;
    ace-server)
        curl_with_local_role_cert /etc/puppetlabs/ace-server/ssl \
            https://127.0.0.1:44633/admin/status
        ;;
    *)
        exit 1
        ;;
esac
