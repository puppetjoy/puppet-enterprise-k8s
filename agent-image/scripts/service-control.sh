#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-agent-common.sh

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
    if [ "${service_name}" = "pxp-agent" ]; then
        case "${action}" in
            is-active)
                if pxp_agent_running; then
                    printf '%s\n' active
                else
                    printf '%s\n' inactive
                fi
                ;;
            status)
                if pxp_agent_running; then
                    printf '%s\n' 'pxp-agent.service - active (running)'
                else
                    printf '%s\n' 'pxp-agent.service - inactive'
                fi
                ;;
        esac
        return 0
    fi

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
    start)
        if [ "${service_name}" = "pxp-agent" ]; then
            start_pxp_agent
        fi
        exit 0
        ;;
    stop)
        if [ "${service_name}" = "pxp-agent" ]; then
            stop_pxp_agent
        fi
        exit 0
        ;;
    restart|reload|try-restart|condrestart)
        if [ "${service_name}" = "pxp-agent" ]; then
            stop_pxp_agent
            start_pxp_agent
        fi
        exit 0
        ;;
    status|is-active)
        emit_status
        if [ "${service_name}" = "pxp-agent" ] && ! pxp_agent_running; then
            exit 3
        fi
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
