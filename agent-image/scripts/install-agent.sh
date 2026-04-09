#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-agent-common.sh

ensure_puppet_agent_installed
write_puppet_conf
log "Agent prerequisites are ready"
