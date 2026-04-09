#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

command="${1:-help}"
shift || true

case "${command}" in
    install)
        exec /usr/local/bin/install-pe.sh "$@"
        ;;
    regenerate-cert)
        exec /usr/local/bin/regenerate-cert.sh "$@"
        ;;
    recover-cert)
        exec /usr/local/bin/recover-cert.sh "$@"
        ;;
    run-role)
        exec /usr/local/bin/run-role.sh "$@"
        ;;
    wait-for-install)
        exec /usr/local/bin/wait-for-install.sh "$@"
        ;;
    probe)
        exec /usr/local/bin/probe.sh "$@"
        ;;
    help|*)
        cat <<'EOF'
Usage:
  pe-k8s-entrypoint install
  pe-k8s-entrypoint regenerate-cert
  pe-k8s-entrypoint recover-cert
  pe-k8s-entrypoint run-role <role>
  pe-k8s-entrypoint wait-for-install
  pe-k8s-entrypoint probe <role>

Roles:
  postgresql
  puppetdb
  puppetserver
  nginx
  console-services
  orchestration-services
  host-action-collector
  bolt-server
  ace-server
EOF
        ;;
esac
