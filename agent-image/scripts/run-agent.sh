#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-agent-common.sh

ensure_puppet_agent_installed
write_puppet_conf
bootstrap_agent_ssl
run_initial_agent_convergence

if ! start_pxp_agent; then
    log "PXP agent is not running after initial convergence"
fi

log "Starting puppet agent for $(agent_certname) against ${PE_AGENT_SERVER}"
exec /opt/puppetlabs/bin/puppet agent --no-daemonize --verbose
