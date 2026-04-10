#!/usr/bin/env python3

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def log(message):
    print(f"[service-selector] {message}", flush=True)


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


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

    def patch_pod_labels(self, pod_name, labels):
        self.request(
            "PATCH",
            f"/api/v1/namespaces/{self.namespace}/pods/{pod_name}",
            payload={"metadata": {"labels": labels}},
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

    def select_active_pod(self, pods):
        candidates = []
        labeled_candidates = []
        for pod in pods:
            metadata = pod.get("metadata", {})
            name = metadata.get("name", "")
            ordinal = pod_ordinal(name, self.statefulset_name)
            if ordinal is None:
                continue
            if not pod_ready(pod):
                continue
            candidates.append((ordinal, name))
            labels = metadata.get("labels", {})
            if labels.get(self.active_label_key) == self.active_label_value:
                labeled_candidates.append((ordinal, name))
        if labeled_candidates:
            labeled_candidates.sort()
            return labeled_candidates[0][1]
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def reconcile(self):
        pods = self.k8s.list_pods(label_selector=self.label_selector)
        current_pod = None
        for pod in pods:
            if pod.get("metadata", {}).get("name") == self.pod_name:
                current_pod = pod
                break
        if current_pod is None:
            raise RuntimeError(f"Unable to find current pod {self.namespace}/{self.pod_name}")

        selected_name = self.select_active_pod(pods)
        should_be_active = selected_name == self.pod_name
        existing_labels = current_pod.get("metadata", {}).get("labels", {})
        current_value = existing_labels.get(self.active_label_key)
        desired_value = self.active_label_value if should_be_active else None

        if current_value != desired_value:
            self.k8s.patch_pod_labels(self.pod_name, {self.active_label_key: desired_value})
            action = "activated" if should_be_active else "cleared"
            log(
                f"{action} {self.active_label_key} on {self.namespace}/{self.pod_name}"
            )

        if selected_name != self.last_selected_name:
            if selected_name:
                destination = self.service_name or self.active_label_key
                log(
                    f"Selected active pod {self.namespace}/{selected_name} for {destination}"
                )
            else:
                log(f"No ready control-plane pod available for {self.target_description}")
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
