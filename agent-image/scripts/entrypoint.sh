#!/bin/bash
set -euo pipefail

command="${1:-help}"
shift || true

case "${command}" in
    install)
        exec /usr/local/bin/install-agent.sh "$@"
        ;;
    run)
        exec /usr/local/bin/run-agent.sh "$@"
        ;;
    probe)
        exec /usr/local/bin/probe.sh "$@"
        ;;
    help|*)
        cat <<'EOF'
Usage:
  pe-agent-entrypoint install
  pe-agent-entrypoint run
  pe-agent-entrypoint probe <startup|readiness|liveness>
EOF
        ;;
esac
