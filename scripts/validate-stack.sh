#!/usr/bin/env bash
set -euo pipefail

namespace="${PE_NAMESPACE:-puppet}"
release="${PE_RELEASE:-pe}"
frontdoor_service="${PE_FRONTDOOR_SERVICE:-pe}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[validate] front-door status"
"${repo_root}/scripts/pe-frontdoor-status.sh" "${namespace}" "${release}" "${frontdoor_service}"

echo ""
echo "[validate] HA failover"
PE_NAMESPACE="${namespace}" \
PE_RELEASE="${release}" \
"${repo_root}/scripts/validate-pe-failover.sh"
