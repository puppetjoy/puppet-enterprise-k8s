#!/bin/bash
set -euo pipefail

program="$(basename "$0")"

parse_args() {
    if [ "${program}" = "service" ]; then
        service_name="${1:-}"
        action="${2:-status}"
    else
        action="${1:-status}"
        service_name="${2:-}"
    fi
}

emit_status() {
    case "${action}" in
        is-active)
            printf '%s\n' active
            ;;
        status)
            printf '%s\n' "${service_name:-service}.service - fake active service"
            ;;
    esac
}

parse_args "$@"

case "${action}" in
    daemon-reload|enable|disable|preset|reset-failed)
        exit 0
        ;;
    start|restart|reload|try-restart|condrestart|stop|status|is-active)
        emit_status
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
