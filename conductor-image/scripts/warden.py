#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


CONDUCTOR_LABEL_KEY = "pe-k8s.puppet.com/conductor"
ONBOARDING_LABEL_VALUE = "onboarding-bundle"
TRUST_SOURCE_LABEL_VALUE = "trust-source"
TRUST_BUNDLE_LABEL_VALUE = "trust-bundle"
API_VERSION = "pe-k8s/v1alpha1"
KIND = "ConductorOnboardingBundle"


def log(message):
    print(f"[warden] {message}", flush=True)


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def sanitize_fragment(value):
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "default"


def participant_secret_name(prefix, segment_name, participant_name, suffix):
    parts = [
        sanitize_fragment(prefix),
        sanitize_fragment(segment_name),
        sanitize_fragment(participant_name),
        sanitize_fragment(suffix),
    ]
    name = "-".join(part for part in parts if part)
    return name[:63].rstrip("-")


def segment_secret_name(prefix, segment_name, suffix):
    parts = [
        sanitize_fragment(prefix),
        sanitize_fragment(segment_name),
        sanitize_fragment(suffix),
    ]
    name = "-".join(part for part in parts if part)
    return name[:63].rstrip("-")


def participant_username(segment_name, participant_name):
    return f"{sanitize_fragment(segment_name)}__{sanitize_fragment(participant_name)}"


def participant_queue(participant_name):
    return f"participant.{sanitize_fragment(participant_name)}"


def b64encode_text(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def b64decode_text(value):
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_pem_block(block):
    return block.strip() + "\n"


def pem_blocks(pem_text, block_type):
    pattern = re.compile(
        rf"-----BEGIN {re.escape(block_type)}-----.*?-----END {re.escape(block_type)}-----\s*",
        re.DOTALL,
    )
    return [normalize_pem_block(match.group(0)) for match in pattern.finditer(pem_text or "")]


def unique_pem_blocks(blocks):
    seen = set()
    unique = []
    for block in blocks:
        normalized = normalize_pem_block(block)
        fingerprint = sha256_text(normalized)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(normalized)
    return unique


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


class RabbitMqApi:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _quote(value):
        return urllib.parse.quote(value, safe="")

    def request(self, method, path, payload=None, expected=None):
        status, body = http_request(
            method,
            f"{self.base_url}{path}",
            headers=self.headers,
            payload=payload,
        )
        if expected and status not in expected:
            raise RuntimeError(f"RabbitMQ API {method} {path} returned {status}: {body}")
        return status, parse_json(body)

    def ensure_vhost(self, name):
        self.request("PUT", f"/api/vhosts/{self._quote(name)}", expected={201, 204})

    def ensure_user(self, username, password):
        self.request(
            "PUT",
            f"/api/users/{self._quote(username)}",
            payload={"password": password, "tags": ""},
            expected={201, 204},
        )

    def delete_user(self, username):
        self.request("DELETE", f"/api/users/{self._quote(username)}", expected={204, 404})

    def list_users(self):
        _, data = self.request("GET", "/api/users", expected={200})
        return data or []

    def ensure_permissions(self, vhost, username):
        self.request(
            "PUT",
            f"/api/permissions/{self._quote(vhost)}/{self._quote(username)}",
            payload={"configure": ".*", "write": ".*", "read": ".*"},
            expected={201, 204},
        )

    def ensure_exchange(self, vhost, name, exchange_type):
        self.request(
            "PUT",
            f"/api/exchanges/{self._quote(vhost)}/{self._quote(name)}",
            payload={
                "type": exchange_type,
                "durable": True,
                "auto_delete": False,
                "internal": False,
                "arguments": {},
            },
            expected={201, 204},
        )

    def ensure_queue(self, vhost, name):
        self.request(
            "PUT",
            f"/api/queues/{self._quote(vhost)}/{self._quote(name)}",
            payload={"durable": True, "auto_delete": False, "arguments": {}},
            expected={201, 204},
        )

    def delete_queue(self, vhost, name):
        self.request(
            "DELETE",
            f"/api/queues/{self._quote(vhost)}/{self._quote(name)}",
            expected={204, 404},
        )

    def list_queues(self, vhost):
        _, data = self.request(
            "GET",
            f"/api/queues/{self._quote(vhost)}",
            expected={200},
        )
        return data or []

    def ensure_queue_binding(self, vhost, exchange, queue, routing_key):
        _, bindings = self.request(
            "GET",
            f"/api/bindings/{self._quote(vhost)}/e/{self._quote(exchange)}/q/{self._quote(queue)}",
            expected={200},
        )
        bindings = bindings or []
        if any(binding.get("routing_key") == routing_key for binding in bindings):
            return
        self.request(
            "POST",
            f"/api/bindings/{self._quote(vhost)}/e/{self._quote(exchange)}/q/{self._quote(queue)}",
            payload={"routing_key": routing_key, "arguments": {}},
            expected={201},
        )


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
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

    def request(self, method, path, payload=None, expected=None):
        status, body = http_request(
            method,
            f"{self.base_url}{path}",
            headers=self.headers,
            payload=payload,
            context=self.context,
        )
        if expected and status not in expected:
            raise RuntimeError(f"Kubernetes API {method} {path} returned {status}: {body}")
        return status, parse_json(body)

    def _namespaced_path(self, namespace, resource, name=None, api_group="api/v1"):
        namespace = namespace or self.namespace
        path = f"/{api_group}/namespaces/{namespace}/{resource}"
        if name:
            path = f"{path}/{name}"
        return path

    def get_secret(self, name, namespace=None):
        status, data = self.request(
            "GET",
            self._namespaced_path(namespace, "secrets", name=name),
            expected={200, 404},
        )
        return data if status == 200 else None

    def list_secrets(self, label_selector="", namespace=None):
        path = self._namespaced_path(namespace, "secrets")
        if label_selector:
            query = urllib.parse.urlencode({"labelSelector": label_selector})
            path = f"{path}?{query}"
        _, data = self.request("GET", path, expected={200})
        return (data or {}).get("items", [])

    def delete_secret(self, name, namespace=None):
        self.request(
            "DELETE",
            self._namespaced_path(namespace, "secrets", name=name),
            expected={200, 202, 404},
        )

    def upsert_secret(self, name, labels, annotations, string_data, namespace=None):
        namespace = namespace or self.namespace
        desired = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
                "annotations": annotations,
            },
            "type": "Opaque",
            "data": {key: b64encode_text(value) for key, value in string_data.items()},
        }
        existing = self.get_secret(name, namespace=namespace)
        if existing is not None:
            existing_data = existing.get("data", {})
            existing_labels = existing.get("metadata", {}).get("labels", {})
            existing_annotations = existing.get("metadata", {}).get("annotations", {})
            if (
                existing_data == desired["data"]
                and existing_labels == labels
                and existing_annotations == annotations
            ):
                return False
            desired["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
            self.request(
                "PUT",
                self._namespaced_path(namespace, "secrets", name=name),
                payload=desired,
                expected={200},
            )
            return True
        self.request(
            "POST",
            self._namespaced_path(namespace, "secrets"),
            payload=desired,
            expected={201},
        )
        return True

    def get_statefulset(self, name, namespace=None):
        status, data = self.request(
            "GET",
            self._namespaced_path(namespace, "statefulsets", name=name, api_group="apis/apps/v1"),
            expected={200, 404},
        )
        return data if status == 200 else None


def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def existing_password(secret):
    if not secret:
        return ""
    data = secret.get("data", {})
    encoded = data.get("password")
    if not encoded:
        return ""
    return b64decode_text(encoded)


def build_bundle(config, segment, participant, username, password):
    host = config["hub"]["host"]
    amqp_port = config["hub"]["amqpPort"]
    management_port = config["hub"]["managementPort"]
    data_exchange = config["exchanges"]["data"]
    control_exchange = config["exchanges"]["control"]
    warden_exchange = config["exchanges"]["wardenWork"]
    queue_name = participant_queue(participant["name"])
    bundle = {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "segment": {
            "name": segment["name"],
            "vhost": segment["vhost"],
            "pkiDomain": segment.get("pkiDomain", ""),
            "region": segment.get("region", ""),
        },
        "participant": {
            "name": participant["name"],
            "role": participant.get("role", "worker"),
            "username": username,
        },
        "hub": {
            "host": host,
            "amqpPort": amqp_port,
            "managementUrl": f"http://{host}:{management_port}",
            "vhost": segment["vhost"],
            "credentials": {
                "username": username,
                "password": password,
            },
            "exchanges": {
                "data": data_exchange,
                "control": control_exchange,
                "wardenWork": warden_exchange,
            },
            "queue": queue_name,
            "routingKeys": [
                "broadcast.#",
                f"participant.{sanitize_fragment(participant['name'])}.#",
            ],
        },
    }
    topology = {}
    for key in ("namespace", "releaseName", "workloadSet", "workloadKind", "workloadName", "ordinal"):
        if participant.get(key, "") != "":
            topology[key] = participant[key]
    if topology:
        bundle["topology"] = topology
    return bundle


def build_static_participants(default_namespace, segment):
    participants = []
    for participant in segment.get("participants", []):
        participant_copy = dict(participant)
        participant_copy.setdefault("namespace", default_namespace)
        participants.append(participant_copy)
    return participants


def build_release_domain_participants(k8s, default_namespace, domain):
    namespace = domain.get("namespace") or default_namespace
    release_name = domain.get("releaseName", "")
    participants = []
    for workload_set in domain.get("workloadSets", []):
        if not workload_set.get("enabled", True):
            continue

        kind = workload_set.get("kind", "StatefulSet")
        if kind.lower() != "statefulset":
            log(
                f"Skipping unsupported workload kind {kind!r} in release domain {release_name or '<unnamed>'}"
            )
            continue

        workload_name = workload_set.get("resourceName", "").strip()
        if not workload_name:
            log(f"Skipping workload set without resourceName in release domain {release_name or '<unnamed>'}")
            continue

        statefulset = k8s.get_statefulset(workload_name, namespace=namespace)
        if statefulset is None:
            if not workload_set.get("optional", False):
                log(
                    f"Workload set {workload_set.get('name', workload_name)!r} targets missing StatefulSet "
                    f"{workload_name!r} in namespace {namespace!r}"
                )
            continue

        replicas = int((statefulset.get("spec") or {}).get("replicas") or 0)
        prefix = workload_set.get("participantNamePrefix") or workload_name
        role = workload_set.get("role", "worker")
        workload_set_name = workload_set.get("name", prefix)

        for ordinal in range(replicas):
            participants.append(
                {
                    "name": f"{prefix}-{ordinal}",
                    "role": role,
                    "namespace": namespace,
                    "releaseName": release_name,
                    "workloadSet": workload_set_name,
                    "workloadKind": "StatefulSet",
                    "workloadName": workload_name,
                    "ordinal": ordinal,
                }
            )

    return participants


def desired_participants(k8s, default_namespace, segment):
    desired = {}
    for participant in build_static_participants(default_namespace, segment):
        desired[participant["name"]] = participant
    for domain in segment.get("releaseDomains", []):
        for participant in build_release_domain_participants(k8s, default_namespace, domain):
            desired[participant["name"]] = participant
    return list(desired.values())


def participant_namespaces(default_namespace, segment, participants):
    namespaces = set()
    for participant in participants:
        namespaces.add(participant.get("namespace") or default_namespace)
    for domain in segment.get("releaseDomains", []):
        namespaces.add(domain.get("namespace") or default_namespace)
    return namespaces or {default_namespace}


def decode_json_field(secret, key):
    data = secret.get("data", {})
    if key not in data:
        return {}
    try:
        return json.loads(b64decode_text(data[key]))
    except json.JSONDecodeError:
        return {}


def load_trust_source(secret):
    data = secret.get("data", {})
    ca_pem = b64decode_text(data["ca.pem"]) if "ca.pem" in data else ""
    crl_pem = b64decode_text(data["crl.pem"]) if "crl.pem" in data else ""
    ca_blocks = unique_pem_blocks(pem_blocks(ca_pem, "CERTIFICATE"))
    crl_blocks = unique_pem_blocks(pem_blocks(crl_pem, "X509 CRL"))
    metadata = decode_json_field(secret, "metadata.json")
    secret_metadata = secret.get("metadata", {})
    labels = secret_metadata.get("labels", {})
    annotations = secret_metadata.get("annotations", {})
    summary = {
        "participant": metadata.get("participant") or labels.get("pe-k8s.puppet.com/participant", ""),
        "role": metadata.get("role") or annotations.get("pe-k8s.puppet.com/participant-role", ""),
        "namespace": metadata.get("namespace", ""),
        "segment": metadata.get("segment", ""),
        "caBlockCount": len(ca_blocks),
        "crlBlockCount": len(crl_blocks),
        "caSha256": sha256_text("".join(ca_blocks)) if ca_blocks else "",
        "crlSha256": sha256_text("".join(crl_blocks)) if crl_blocks else "",
    }
    return summary, ca_blocks, crl_blocks


def prune_stale_participants(
    k8s,
    rabbit,
    default_namespace,
    segment,
    participants,
    desired_secret_refs,
    desired_usernames,
    desired_queue_names,
):
    segment_key = sanitize_fragment(segment["name"])
    label_selector = (
        f"{CONDUCTOR_LABEL_KEY}={ONBOARDING_LABEL_VALUE},"
        f"pe-k8s.puppet.com/fabric-segment={segment_key}"
    )
    for namespace in participant_namespaces(default_namespace, segment, participants):
        for secret in k8s.list_secrets(label_selector, namespace=namespace):
            secret_name = secret["metadata"]["name"]
            secret_ref = (namespace, secret_name)
            if secret_ref in desired_secret_refs:
                continue
            k8s.delete_secret(secret_name, namespace=namespace)
            log(f"Pruned stale onboarding bundle {secret_name} from namespace {namespace}")

    username_prefix = f"{segment_key}__"
    for user in rabbit.list_users():
        username = user.get("name", "")
        if username.startswith(username_prefix) and username not in desired_usernames:
            rabbit.delete_user(username)
            log(f"Pruned stale RabbitMQ user {username}")

    for queue in rabbit.list_queues(segment["vhost"]):
        queue_name = queue.get("name", "")
        if queue_name.startswith("participant.") and queue_name not in desired_queue_names:
            rabbit.delete_queue(segment["vhost"], queue_name)
            log(f"Pruned stale RabbitMQ queue {queue_name} from {segment['vhost']}")


def reconcile_trust_bundle(k8s, prefix, default_namespace, segment, participants, prune_stale):
    segment_name = segment["name"]
    segment_key = sanitize_fragment(segment_name)
    trust_namespace = default_namespace
    desired_source_names = set()
    source_summaries = []
    ca_blocks = []
    crl_blocks = []

    for participant in participants:
        if participant.get("role", "") != "control-plane":
            continue
        secret_name = participant_secret_name(prefix, segment_name, participant["name"], "trust-source")
        desired_source_names.add(secret_name)
        secret = k8s.get_secret(secret_name, namespace=trust_namespace)
        if secret is None:
            continue

        summary, source_ca_blocks, source_crl_blocks = load_trust_source(secret)
        if not source_ca_blocks or not source_crl_blocks:
            log(f"Skipping incomplete trust source {secret_name} in namespace {trust_namespace}")
            continue

        summary["participant"] = participant["name"]
        summary["role"] = participant.get("role", "")
        if participant.get("releaseName"):
            summary["releaseName"] = participant["releaseName"]
        if participant.get("workloadName"):
            summary["workloadName"] = participant["workloadName"]
        source_summaries.append(summary)
        ca_blocks.extend(source_ca_blocks)
        crl_blocks.extend(source_crl_blocks)

    if prune_stale:
        label_selector = (
            f"{CONDUCTOR_LABEL_KEY}={TRUST_SOURCE_LABEL_VALUE},"
            f"pe-k8s.puppet.com/fabric-segment={segment_key}"
        )
        for secret in k8s.list_secrets(label_selector, namespace=trust_namespace):
            secret_name = secret["metadata"]["name"]
            if secret_name in desired_source_names:
                continue
            k8s.delete_secret(secret_name, namespace=trust_namespace)
            log(f"Pruned stale trust source {secret_name} from namespace {trust_namespace}")

    bundle_name = segment_secret_name(prefix, segment_name, "trust-bundle")
    if not source_summaries:
        if prune_stale and k8s.get_secret(bundle_name, namespace=trust_namespace) is not None:
            k8s.delete_secret(bundle_name, namespace=trust_namespace)
            log(f"Pruned empty trust bundle {bundle_name} from namespace {trust_namespace}")
        return

    unique_ca_blocks = unique_pem_blocks(ca_blocks)
    unique_crl_blocks = unique_pem_blocks(crl_blocks)
    metadata = {
        "apiVersion": API_VERSION,
        "kind": "ConductorTrustBundle",
        "segment": {
            "name": segment_name,
            "vhost": segment["vhost"],
            "pkiDomain": segment.get("pkiDomain", ""),
            "region": segment.get("region", ""),
        },
        "generatedAt": int(time.time()),
        "sourceCount": len(source_summaries),
        "caBlockCount": len(unique_ca_blocks),
        "crlBlockCount": len(unique_crl_blocks),
        "sources": source_summaries,
    }
    updated = k8s.upsert_secret(
        bundle_name,
        labels={
            CONDUCTOR_LABEL_KEY: TRUST_BUNDLE_LABEL_VALUE,
            "pe-k8s.puppet.com/fabric-segment": segment_key,
        },
        annotations={
            "pe-k8s.puppet.com/trust-source-count": str(len(source_summaries)),
        },
        string_data={
            "ca.pem": "".join(unique_ca_blocks),
            "crl.pem": "".join(unique_crl_blocks),
            "metadata.json": json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        },
        namespace=trust_namespace,
    )
    if updated:
        log(f"Reconciled trust bundle {bundle_name} for segment {segment_name}")


def reconcile_segment(k8s, rabbit, config, prefix, default_namespace, segment, prune_stale):
    segment_name = segment["name"]
    vhost = segment["vhost"]
    rabbit.ensure_vhost(vhost)
    rabbit.ensure_exchange(vhost, config["exchanges"]["data"], "topic")
    rabbit.ensure_exchange(vhost, config["exchanges"]["control"], "topic")
    rabbit.ensure_exchange(vhost, config["exchanges"]["wardenWork"], "direct")

    control_queue = f"warden.{sanitize_fragment(segment_name)}.control"
    work_queue = f"warden.{sanitize_fragment(segment_name)}.work"
    rabbit.ensure_queue(vhost, control_queue)
    rabbit.ensure_queue(vhost, work_queue)
    rabbit.ensure_queue_binding(vhost, config["exchanges"]["control"], control_queue, "#")
    rabbit.ensure_queue_binding(
        vhost,
        config["exchanges"]["wardenWork"],
        work_queue,
        sanitize_fragment(segment_name),
    )

    participants = desired_participants(k8s, default_namespace, segment)
    desired_secret_refs = set()
    desired_usernames = set()
    desired_queue_names = set()

    for participant in participants:
        namespace = participant.get("namespace") or default_namespace
        name = participant["name"]
        secret_name = participant_secret_name(prefix, segment_name, name, "onboarding")
        secret = k8s.get_secret(secret_name, namespace=namespace)
        password = existing_password(secret) or secrets.token_urlsafe(24)
        username = participant.get("username") or participant_username(segment_name, name)
        queue_name = participant_queue(name)

        desired_secret_refs.add((namespace, secret_name))
        desired_usernames.add(username)
        desired_queue_names.add(queue_name)

        rabbit.ensure_user(username, password)
        rabbit.ensure_permissions(vhost, username)
        rabbit.ensure_queue(vhost, queue_name)
        rabbit.ensure_queue_binding(vhost, config["exchanges"]["data"], queue_name, "broadcast.#")
        rabbit.ensure_queue_binding(
            vhost,
            config["exchanges"]["data"],
            queue_name,
            f"participant.{sanitize_fragment(name)}.#",
        )

        bundle = build_bundle(config, segment, participant, username, password)
        annotations = {
            "pe-k8s.puppet.com/participant-role": participant.get("role", "worker"),
        }
        if participant.get("releaseName"):
            annotations["pe-k8s.puppet.com/release-name"] = participant["releaseName"]
        if participant.get("workloadSet"):
            annotations["pe-k8s.puppet.com/workload-set"] = participant["workloadSet"]
        if participant.get("workloadName"):
            annotations["pe-k8s.puppet.com/workload-name"] = participant["workloadName"]

        updated = k8s.upsert_secret(
            secret_name,
            labels={
                CONDUCTOR_LABEL_KEY: ONBOARDING_LABEL_VALUE,
                "pe-k8s.puppet.com/fabric-segment": sanitize_fragment(segment_name),
                "pe-k8s.puppet.com/participant": sanitize_fragment(name),
            },
            annotations=annotations,
            string_data={
                "username": username,
                "password": password,
                "onboarding.json": json.dumps(bundle, indent=2, sort_keys=True) + "\n",
            },
            namespace=namespace,
        )
        if updated:
            log(f"Reconciled onboarding bundle {secret_name} for {name} in segment {segment_name}")

    reconcile_trust_bundle(k8s, prefix, default_namespace, segment, participants, prune_stale)

    if prune_stale:
        prune_stale_participants(
            k8s,
            rabbit,
            default_namespace,
            segment,
            participants,
            desired_secret_refs,
            desired_usernames,
            desired_queue_names,
        )


def reconcile():
    config = load_config(os.environ.get("WARDEN_CONFIG_PATH", "/config/config.json"))
    namespace = os.environ["WARDEN_NAMESPACE"]
    prefix = os.environ.get("WARDEN_RESOURCE_PREFIX", "conductor")
    prune_stale = env_bool("WARDEN_PRUNE_STALE_PARTICIPANTS", True)
    k8s = K8sApi(namespace)
    rabbit = RabbitMqApi(
        os.environ["WARDEN_RABBITMQ_API_URL"],
        os.environ["WARDEN_RABBITMQ_USERNAME"],
        os.environ["WARDEN_RABBITMQ_PASSWORD"],
    )
    for segment in config.get("segments", []):
        reconcile_segment(k8s, rabbit, config, prefix, namespace, segment, prune_stale)


def main():
    interval = env_int("WARDEN_INTERVAL_SECONDS", 30)
    while True:
        try:
            reconcile()
        except KeyboardInterrupt:
            raise
        except Exception as error:
            log(f"Reconcile failed: {error}")
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
