#!/usr/bin/env python3

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FRONTDOOR_ELIGIBLE_ANNOTATION = "pe-k8s.puppet.com/frontdoor-eligible"
FRONTDOOR_BLOCKERS_ANNOTATION = "pe-k8s.puppet.com/frontdoor-blockers"
FRONTDOOR_REASON_ANNOTATION = "pe-k8s.puppet.com/frontdoor-reason"
FRONTDOOR_UPDATED_AT_ANNOTATION = "pe-k8s.puppet.com/frontdoor-updated-at"
FRONTDOOR_STATE_ANNOTATION = "pe-k8s.puppet.com/frontdoor-state"
FRONTDOOR_SELECTION_MODE_ANNOTATION = "pe-k8s.puppet.com/frontdoor-selection-mode"
FRONTDOOR_SELECTION_DETAIL_ANNOTATION = "pe-k8s.puppet.com/frontdoor-selection-detail"


def log(message):
    print(f"[service-selector] {message}", flush=True)


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def parse_int(value, default=0):
    try:
        return int((value or "").strip())
    except (AttributeError, TypeError, ValueError):
        return default


def http_request(method, url, headers=None, payload=None, context=None):
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    request = urllib.request.Request(url, method=method, data=data)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, context=context, timeout=30) as response:
            body = response.read().decode("utf-8")
            return response.status, body
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        return error.code, body


def parse_json(body):
    if not body:
        return None
    return json.loads(body)


def pod_ready(pod):
    metadata = pod.get("metadata", {})
    if metadata.get("deletionTimestamp"):
        return False
    status = pod.get("status", {})
    if status.get("phase") != "Running":
        return False
    for condition in status.get("conditions", []):
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def pod_ordinal(name, statefulset_name):
    prefix = f"{statefulset_name}-"
    if not name.startswith(prefix):
        return None
    suffix = name[len(prefix):]
    if not suffix.isdigit():
        return None
    return int(suffix)


class K8sApi:
    def __init__(self, namespace):
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
        port = os.environ.get(
            "KUBERNETES_SERVICE_PORT_HTTPS",
            os.environ.get("KUBERNETES_SERVICE_PORT", "443"),
        ).strip()
        if not host:
            raise RuntimeError("KUBERNETES_SERVICE_HOST is not set")
        self.namespace = namespace
        self.base_url = f"https://{host}:{port}"
        with open("/var/run/secrets/kubernetes.io/serviceaccount/token", "r", encoding="utf-8") as handle:
            token = handle.read().strip()
        self.default_headers = {
            "Authorization": f"Bearer {token}",
        }
        self.context = ssl.create_default_context(
            cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        )

    def request(self, method, path, payload=None, expected=None, content_type="application/json"):
        headers = dict(self.default_headers)
        if payload is not None:
            headers["Content-Type"] = content_type
        status, body = http_request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            payload=payload,
            context=self.context,
        )
        if expected and status not in expected:
            raise RuntimeError(f"Kubernetes API {method} {path} returned {status}: {body}")
        return status, parse_json(body)

    def list_pods(self, label_selector=""):
        path = f"/api/v1/namespaces/{self.namespace}/pods"
        if label_selector:
            path = f"{path}?{urllib.parse.urlencode({'labelSelector': label_selector})}"
        _, data = self.request("GET", path, expected={200})
        return (data or {}).get("items", [])

    def patch_pod_metadata(self, pod_name, *, labels=None, annotations=None):
        metadata = {}
        if labels is not None:
            metadata["labels"] = labels
        if annotations is not None:
            metadata["annotations"] = annotations
        if not metadata:
            return
        self.request(
            "PATCH",
            f"/api/v1/namespaces/{self.namespace}/pods/{pod_name}",
            payload={"metadata": metadata},
            expected={200},
            content_type="application/merge-patch+json",
        )


class ServiceSelector:
    def __init__(self):
        self.pod_name = os.environ.get("SERVICE_SELECTOR_POD_NAME", "").strip()
        self.namespace = os.environ.get("SERVICE_SELECTOR_POD_NAMESPACE", "").strip()
        self.statefulset_name = os.environ.get("SERVICE_SELECTOR_STATEFULSET_NAME", "").strip()
        self.label_selector = os.environ.get("SERVICE_SELECTOR_LABEL_SELECTOR", "").strip()
        self.active_label_key = os.environ.get("SERVICE_SELECTOR_ACTIVE_LABEL_KEY", "").strip()
        self.active_label_value = os.environ.get("SERVICE_SELECTOR_ACTIVE_LABEL_VALUE", "").strip() or "true"
        self.poll_interval_seconds = env_int("SERVICE_SELECTOR_POLL_INTERVAL_SECONDS", 5)
        self.candidate_annotation_key = (
            os.environ.get("SERVICE_SELECTOR_CANDIDATE_ANNOTATION_KEY", "").strip()
            or FRONTDOOR_ELIGIBLE_ANNOTATION
        )
        self.candidate_annotation_value = (
            os.environ.get("SERVICE_SELECTOR_CANDIDATE_ANNOTATION_VALUE", "").strip()
            or "true"
        )
        self.blockers_annotation_key = (
            os.environ.get("SERVICE_SELECTOR_BLOCKERS_ANNOTATION_KEY", "").strip()
            or FRONTDOOR_BLOCKERS_ANNOTATION
        )
        self.reason_annotation_key = (
            os.environ.get("SERVICE_SELECTOR_REASON_ANNOTATION_KEY", "").strip()
            or FRONTDOOR_REASON_ANNOTATION
        )
        self.updated_at_annotation_key = (
            os.environ.get("SERVICE_SELECTOR_UPDATED_AT_ANNOTATION_KEY", "").strip()
            or FRONTDOOR_UPDATED_AT_ANNOTATION
        )
        self.candidate_max_age_seconds = env_int(
            "SERVICE_SELECTOR_CANDIDATE_MAX_AGE_SECONDS",
            60,
        )
        self.state_annotation_key = (
            os.environ.get("SERVICE_SELECTOR_STATE_ANNOTATION_KEY", "").strip()
            or FRONTDOOR_STATE_ANNOTATION
        )
        self.selection_mode_annotation_key = (
            os.environ.get("SERVICE_SELECTOR_SELECTION_MODE_ANNOTATION_KEY", "").strip()
            or FRONTDOOR_SELECTION_MODE_ANNOTATION
        )
        self.selection_detail_annotation_key = (
            os.environ.get("SERVICE_SELECTOR_SELECTION_DETAIL_ANNOTATION_KEY", "").strip()
            or FRONTDOOR_SELECTION_DETAIL_ANNOTATION
        )
        self.service_name = os.environ.get("SERVICE_SELECTOR_SERVICE_NAME", "").strip()
        self.target_description = (
            os.environ.get("SERVICE_SELECTOR_TARGET_DESCRIPTION", "").strip()
            or self.service_name
            or self.active_label_key
        )

        for key, value in (
            ("SERVICE_SELECTOR_POD_NAME", self.pod_name),
            ("SERVICE_SELECTOR_POD_NAMESPACE", self.namespace),
            ("SERVICE_SELECTOR_STATEFULSET_NAME", self.statefulset_name),
            ("SERVICE_SELECTOR_ACTIVE_LABEL_KEY", self.active_label_key),
        ):
            if not value:
                raise RuntimeError(f"{key} is required")

        self.k8s = K8sApi(self.namespace)
        self.last_selected_name = None
        self.last_active = None

    def pod_selection_info(self, pod):
        metadata = pod.get("metadata", {})
        name = metadata.get("name", "")
        ordinal = pod_ordinal(name, self.statefulset_name)
        labels = metadata.get("labels", {})
        annotations = metadata.get("annotations", {})
        ready = pod_ready(pod)
        updated_at = parse_int(annotations.get(self.updated_at_annotation_key), 0)
        status_seen = (
            self.candidate_annotation_key in annotations
            or self.updated_at_annotation_key in annotations
        )
        status_age_seconds = None
        if updated_at > 0:
            status_age_seconds = max(0, int(time.time()) - updated_at)
        status_fresh = updated_at > 0 and status_age_seconds is not None and (
            status_age_seconds <= self.candidate_max_age_seconds
        )
        eligible = (
            annotations.get(self.candidate_annotation_key) == self.candidate_annotation_value
            and status_fresh
        )
        blockers = (annotations.get(self.blockers_annotation_key) or "").strip()
        reason = (annotations.get(self.reason_annotation_key) or "").strip()

        if status_seen and not status_fresh:
            blockers = blockers or "frontdoor-status-stale"
            reason = reason or blockers
        elif not status_seen:
            blockers = blockers or "frontdoor-status-unpublished"
            reason = reason or blockers
        elif not eligible and not blockers:
            blockers = "frontdoor-not-eligible"
            reason = reason or blockers

        return {
            "name": name,
            "ordinal": ordinal,
            "ready": ready,
            "eligible": eligible,
            "statusSeen": status_seen,
            "statusFresh": status_fresh,
            "statusAgeSeconds": status_age_seconds,
            "blockers": blockers,
            "reason": reason or ("eligible" if eligible else ""),
            "activeLabel": labels.get(self.active_label_key) == self.active_label_value,
        }

    def select_active_pod(self, pod_infos):
        candidates = []
        labeled_candidates = []
        status_seen = any(info["statusSeen"] for info in pod_infos if info["ordinal"] is not None)
        for info in pod_infos:
            ordinal = info["ordinal"]
            if ordinal is None:
                continue
            if not info["ready"]:
                continue
            if status_seen and not info["eligible"]:
                continue
            candidates.append((ordinal, info["name"]))
            if info["activeLabel"]:
                labeled_candidates.append((ordinal, info["name"]))
        if labeled_candidates:
            labeled_candidates.sort()
            return labeled_candidates[0][1], status_seen
        if not candidates:
            return None, status_seen
        candidates.sort()
        return candidates[0][1], status_seen

    def reconcile(self):
        pods = self.k8s.list_pods(label_selector=self.label_selector)
        current_pod = None
        for pod in pods:
            if pod.get("metadata", {}).get("name") == self.pod_name:
                current_pod = pod
                break
        if current_pod is None:
            raise RuntimeError(f"Unable to find current pod {self.namespace}/{self.pod_name}")

        pod_infos = [self.pod_selection_info(pod) for pod in pods]
        current_info = next((info for info in pod_infos if info["name"] == self.pod_name), None)
        selected_name, status_seen = self.select_active_pod(pod_infos)
        should_be_active = selected_name == self.pod_name
        existing_labels = current_pod.get("metadata", {}).get("labels", {})
        existing_annotations = current_pod.get("metadata", {}).get("annotations", {})
        current_value = existing_labels.get(self.active_label_key)
        desired_value = self.active_label_value if should_be_active else None
        if should_be_active:
            desired_state = "active"
            desired_detail = "selected"
        elif not current_info["ready"]:
            desired_state = "waiting"
            desired_detail = "pod-not-ready"
        elif status_seen and not current_info["eligible"]:
            desired_state = "blocked"
            desired_detail = current_info["reason"] or "frontdoor-not-eligible"
        else:
            desired_state = "standby"
            desired_detail = current_info["reason"] or "eligible-standby"
        desired_mode = "gated" if status_seen else "fallback"
        desired_annotations = {
            self.state_annotation_key: desired_state,
            self.selection_mode_annotation_key: desired_mode,
            self.selection_detail_annotation_key: desired_detail,
        }

        if (
            current_value != desired_value
            or existing_annotations.get(self.state_annotation_key) != desired_state
            or existing_annotations.get(self.selection_mode_annotation_key) != desired_mode
            or existing_annotations.get(self.selection_detail_annotation_key) != desired_detail
        ):
            self.k8s.patch_pod_metadata(
                self.pod_name,
                labels={self.active_label_key: desired_value},
                annotations=desired_annotations,
            )
            action = "activated" if should_be_active else "cleared"
            log(f"{action} {self.active_label_key} on {self.namespace}/{self.pod_name}")

        if selected_name != self.last_selected_name:
            if selected_name:
                destination = self.service_name or self.active_label_key
                mode = "gated relay status" if status_seen else "pod readiness fallback"
                log(
                    f"Selected active pod {self.namespace}/{selected_name} for {destination} using {mode}"
                )
            else:
                details = ", ".join(
                    f"{info['name']}={info['reason'] or ('ready' if info['ready'] else 'pod-not-ready')}"
                    for info in sorted(
                        (item for item in pod_infos if item["ordinal"] is not None),
                        key=lambda item: item["ordinal"],
                    )
                )
                log(
                    f"No eligible control-plane pod available for {self.target_description}"
                    + (f" ({details})" if details else "")
                )
            self.last_selected_name = selected_name

        if should_be_active != self.last_active:
            state = "active" if should_be_active else "standby"
            log(f"{self.namespace}/{self.pod_name} is now {state} for {self.target_description}")
            self.last_active = should_be_active

    def run(self):
        while True:
            try:
                self.reconcile()
            except Exception as error:
                log(f"Reconcile failed: {error}")
            time.sleep(self.poll_interval_seconds)


def main():
    selector = ServiceSelector()
    selector.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
