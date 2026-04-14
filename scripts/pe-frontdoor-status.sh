#!/usr/bin/env bash
set -euo pipefail

namespace="${1:-${PE_NAMESPACE:-puppet}}"
release="${2:-${PE_RELEASE:-pe}}"
service="${3:-${PE_FRONTDOOR_SERVICE:-pe}}"

pods_json="$(mktemp)"
endpoints_json="$(mktemp)"
trap 'rm -f "$pods_json" "$endpoints_json"' EXIT

kubectl -n "$namespace" get pods \
  -l "app.kubernetes.io/instance=${release},app.kubernetes.io/component=pe-services" \
  -o json >"$pods_json"
kubectl -n "$namespace" get endpointslice \
  -l "kubernetes.io/service-name=${service}" \
  -o json >"$endpoints_json"

python3 - "$pods_json" "$endpoints_json" "$service" <<'PY'
import json
import sys
from datetime import datetime, timezone

pods_path, endpoints_path, service_name = sys.argv[1:4]

with open(pods_path, "r", encoding="utf-8") as handle:
    pods = json.load(handle)
with open(endpoints_path, "r", encoding="utf-8") as handle:
    endpoint_slices = json.load(handle)

frontdoor = {
    "eligible": "pe-k8s.puppet.com/frontdoor-eligible",
    "blockers": "pe-k8s.puppet.com/frontdoor-blockers",
    "reason": "pe-k8s.puppet.com/frontdoor-reason",
    "updated": "pe-k8s.puppet.com/frontdoor-updated-at",
    "state": "pe-k8s.puppet.com/frontdoor-state",
    "mode": "pe-k8s.puppet.com/frontdoor-selection-mode",
    "detail": "pe-k8s.puppet.com/frontdoor-selection-detail",
    "active": "pe-k8s.puppet.com/frontdoor-active",
}

active_targets = []
for item in endpoint_slices.get("items", []):
    for endpoint in item.get("endpoints", []):
        target_ref = endpoint.get("targetRef") or {}
        active_targets.append(
            {
                "pod": target_ref.get("name", ""),
                "ip": ",".join(endpoint.get("addresses") or []),
                "ready": str((endpoint.get("conditions") or {}).get("ready", "")),
            }
        )


def fmt_epoch(value):
    try:
        epoch = int(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


rows = []
for pod in sorted(
    pods.get("items", []),
    key=lambda item: item.get("metadata", {}).get("name", ""),
):
    metadata = pod.get("metadata", {})
    status = pod.get("status", {})
    labels = metadata.get("labels", {})
    annotations = metadata.get("annotations", {})
    pod_name = metadata.get("name", "")
    ready = "false"
    for condition in status.get("conditions", []):
        if condition.get("type") == "Ready":
            ready = condition.get("status", "").lower()
            break
    rows.append(
        [
            pod_name,
            ready,
            "true" if labels.get(frontdoor["active"]) == "true" else "",
            annotations.get(frontdoor["eligible"], ""),
            annotations.get(frontdoor["state"], ""),
            annotations.get(frontdoor["mode"], ""),
            annotations.get(frontdoor["detail"], ""),
            annotations.get(frontdoor["blockers"], ""),
            fmt_epoch(annotations.get(frontdoor["updated"])),
        ]
    )

headers = ["POD", "READY", "ACTIVE", "ELIGIBLE", "STATE", "MODE", "DETAIL", "BLOCKERS", "UPDATED"]
widths = [len(header) for header in headers]
for row in rows:
    for index, value in enumerate(row):
        widths[index] = max(widths[index], len(value))

print(f"service/{service_name} active endpoints")
if not active_targets:
    print("  none")
else:
    for endpoint in active_targets:
        pod = endpoint["pod"] or "unknown"
        ip = endpoint["ip"] or "unknown"
        ready = endpoint["ready"] or "unknown"
        print(f"  {pod}\t{ip}\tready={ready}")

print("")
print("control-plane pods")
print("  " + "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
for row in rows:
    print("  " + "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
PY
