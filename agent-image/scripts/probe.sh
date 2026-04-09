#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-agent-common.sh

mode="${1:-readiness}"

case "${mode}" in
    startup)
        test -x /opt/puppetlabs/bin/puppet
        test -f /etc/puppetlabs/puppet/puppet.conf
        ;;
    readiness)
        agent_certificate_ready
        if pxp_agent_enabled && pxp_agent_configured; then
            pxp_agent_running
        fi
        ;;
    liveness)
        pgrep -af 'puppet agent' >/dev/null
        if pxp_agent_enabled && pxp_agent_configured; then
            pxp_agent_running
        fi
        ;;
    *)
        log "Unknown probe mode: ${mode}"
        exit 1
        ;;
esac
