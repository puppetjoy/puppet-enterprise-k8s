#!/bin/bash
set -euo pipefail

source /usr/local/lib/pe-k8s-common.sh

wait_for_install_marker
log "Install marker is present"
