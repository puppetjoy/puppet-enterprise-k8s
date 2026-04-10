#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pika


CONDUCTOR_LABEL_KEY = "pe-k8s.puppet.com/conductor"
TRUST_SOURCE_LABEL_VALUE = "trust-source"
DEFAULT_STATUS_FILENAME = "participant-status.json"


def log(message):
    print(f"[participant] {message}", flush=True)


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def sanitize_fragment(value):
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "default"


def b64encode_text(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def b64decode_text(value):
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def normalize_pem_block(block):
    return block.strip() + "\n"


def pem_blocks(pem_text, block_type):
    pattern = re.compile(
        rf"-----BEGIN {re.escape(block_type)}-----.*?-----END {re.escape(block_type)}-----\s*",
        re.DOTALL,
    )
    return [normalize_pem_block(match.group(0)) for match in pattern.finditer(pem_text or "")]


def read_text_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_text_file(path, content):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temp_path, path)


def read_json_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


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


def default_status_path():
    trust_output_dir = os.environ.get("CONDUCTOR_TRUST_OUTPUT_DIR", "").strip()
    base_dir = trust_output_dir or "/tmp"
    return os.environ.get("CONDUCTOR_STATUS_PATH", "").strip() or os.path.join(base_dir, DEFAULT_STATUS_FILENAME)


def probe(mode):
    status_path = default_status_path()
    max_age_seconds = env_int("CONDUCTOR_STATUS_MAX_AGE_SECONDS", 60)

    if not os.path.isfile(status_path):
        return 1

    try:
        status = read_json_file(status_path)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 1
    last_updated_at = int(status.get("lastUpdatedAt") or 0)
    if last_updated_at <= 0:
        return 1

    age_seconds = int(time.time()) - last_updated_at
    if age_seconds > max_age_seconds:
        return 1

    if mode == "live":
        return 0

    if not status.get("onboardingSecretPresent", False):
        return 1
    if not status.get("connected", False):
        return 1
    if not status.get("trustBundleAvailable", False):
        return 1
    if not status.get("trustBundleInstalled", False):
        return 1
    if int(status.get("trustBundleSourceCount") or 0) < 1:
        return 1

    if status.get("trustSourceRequired", False) and not status.get("trustSourcePublished", False):
        return 1

    return 0


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
        if not body:
            return status, None
        return status, json.loads(body)

    def get_secret(self, name, namespace=None):
        namespace = namespace or self.namespace
        path = f"/api/v1/namespaces/{namespace}/secrets/{name}"
        status, data = self.request("GET", path, expected={200, 404})
        return data if status == 200 else None

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
            path = f"/api/v1/namespaces/{namespace}/secrets/{name}"
            self.request("PUT", path, payload=desired, expected={200})
            return True
        path = f"/api/v1/namespaces/{namespace}/secrets"
        self.request("POST", path, payload=desired, expected={201})
        return True


class ParticipantRuntime:
    def __init__(self):
        self.pod_name = os.environ["CONDUCTOR_POD_NAME"].strip()
        self.pod_namespace = os.environ["CONDUCTOR_POD_NAMESPACE"].strip()
        self.conductor_namespace = os.environ.get("CONDUCTOR_NAMESPACE", self.pod_namespace).strip() or self.pod_namespace
        self.resource_prefix = os.environ["CONDUCTOR_RESOURCE_PREFIX"].strip()
        self.segment_name = os.environ["CONDUCTOR_SEGMENT_NAME"].strip()
        self.participant_role = os.environ.get("CONDUCTOR_PARTICIPANT_ROLE", "worker").strip() or "worker"
        self.secret_poll_interval = env_int("CONDUCTOR_SECRET_POLL_INTERVAL_SECONDS", 15)
        self.heartbeat_interval = env_int("CONDUCTOR_HEARTBEAT_INTERVAL_SECONDS", 15)
        self.trust_poll_interval = env_int("CONDUCTOR_TRUST_POLL_INTERVAL_SECONDS", self.secret_poll_interval)
        self.trust_source_ca_path = os.environ.get("CONDUCTOR_TRUST_SOURCE_CA_PATH", "").strip()
        self.trust_source_crl_path = os.environ.get("CONDUCTOR_TRUST_SOURCE_CRL_PATH", "").strip()
        self.trust_output_dir = os.environ.get("CONDUCTOR_TRUST_OUTPUT_DIR", "").strip()
        self.status_path = default_status_path()
        self.trust_source_required = bool(self.trust_source_ca_path and self.trust_source_crl_path)

        self.k8s = K8sApi(self.pod_namespace)
        self.onboarding_secret_name = participant_secret_name(
            self.resource_prefix,
            self.segment_name,
            self.pod_name,
            "onboarding",
        )
        self.trust_source_secret_name = participant_secret_name(
            self.resource_prefix,
            self.segment_name,
            self.pod_name,
            "trust-source",
        )
        self.trust_bundle_secret_name = segment_secret_name(
            self.resource_prefix,
            self.segment_name,
            "trust-bundle",
        )

        self.current_onboarding_fingerprint = ""
        self.current_trust_source_fingerprint = ""
        self.current_trust_bundle_fingerprint = ""
        self.last_secret_poll = 0
        self.last_trust_source_poll = 0
        self.last_trust_bundle_poll = 0
        self.pending_onboarding_warning = False
        self.pending_trust_source_warning = False
        self.pending_trust_bundle_warning = False

        self.connection = None
        self.channel = None
        self.bundle = None
        self.next_heartbeat = 0

        self.status = {
            "participant": self.pod_name,
            "namespace": self.pod_namespace,
            "segment": self.segment_name,
            "role": self.participant_role,
            "onboardingSecretName": self.onboarding_secret_name,
            "trustSourceSecretName": self.trust_source_secret_name,
            "trustBundleSecretName": self.trust_bundle_secret_name,
            "trustSourceRequired": self.trust_source_required,
            "onboardingSecretPresent": False,
            "connected": False,
            "trustSourcePublished": False,
            "trustBundleAvailable": False,
            "trustBundleInstalled": False,
            "trustBundleSourceCount": 0,
            "lastConnectedAt": 0,
            "lastTrustSourcePublishedAt": 0,
            "lastTrustBundleInstalledAt": 0,
            "lastError": "",
            "lastUpdatedAt": 0,
        }

    @staticmethod
    def decode_onboarding_secret(secret):
        data = secret.get("data", {})
        onboarding_json = b64decode_text(data["onboarding.json"])
        username = b64decode_text(data["username"])
        password = b64decode_text(data["password"])
        fingerprint = sha256_text("\n".join([onboarding_json, username, password]))
        bundle = json.loads(onboarding_json)
        return fingerprint, bundle, username, password

    def set_status(self, **updates):
        self.status.update(updates)

    def write_status(self):
        self.status["lastUpdatedAt"] = int(time.time())
        write_text_file(self.status_path, json.dumps(self.status, indent=2, sort_keys=True) + "\n")

    def close_connection(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None
        self.channel = None
        self.bundle = None
        self.set_status(connected=False)

    def on_message(self, channel, method, _properties, body):
        routing_key = method.routing_key or ""
        if routing_key == f"participant.{sanitize_fragment(self.pod_name)}.heartbeat":
            channel.basic_ack(method.delivery_tag)
            return
        log(f"Received {routing_key}: {body.decode('utf-8', errors='replace')}")
        channel.basic_ack(method.delivery_tag)

    def connect(self, bundle, username, password):
        credentials = pika.PlainCredentials(username, password)
        parameters = pika.ConnectionParameters(
            host=bundle["hub"]["host"],
            port=int(bundle["hub"]["amqpPort"]),
            virtual_host=bundle["hub"]["vhost"],
            credentials=credentials,
            heartbeat=max(self.heartbeat_interval * 2, 30),
            blocked_connection_timeout=30,
            client_properties={
                "connection_name": f"{self.pod_namespace}/{self.pod_name}",
            },
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()

        queue_name = bundle["hub"]["queue"]
        data_exchange = bundle["hub"]["exchanges"]["data"]
        control_exchange = bundle["hub"]["exchanges"]["control"]

        channel.exchange_declare(exchange=data_exchange, exchange_type="topic", durable=True)
        channel.exchange_declare(exchange=control_exchange, exchange_type="topic", durable=True)
        channel.queue_declare(queue=queue_name, durable=True)
        for routing_key in bundle["hub"].get("routingKeys", []):
            channel.queue_bind(exchange=data_exchange, queue=queue_name, routing_key=routing_key)
            channel.queue_bind(exchange=control_exchange, queue=queue_name, routing_key=routing_key)
        channel.basic_consume(queue=queue_name, on_message_callback=self.on_message)

        self.connection = connection
        self.channel = channel
        self.bundle = bundle
        self.next_heartbeat = 0
        self.set_status(connected=True, lastConnectedAt=int(time.time()), lastError="")
        log(
            "Connected to Fabric as "
            f"{bundle['participant']['name']} on {bundle['segment']['name']} "
            f"via {bundle['hub']['host']}:{bundle['hub']['amqpPort']}"
        )

    def refresh_onboarding(self, force=False):
        now = time.time()
        if not force and now - self.last_secret_poll < self.secret_poll_interval:
            return
        self.last_secret_poll = now

        secret = self.k8s.get_secret(self.onboarding_secret_name, namespace=self.conductor_namespace)
        if secret is None:
            if not self.pending_onboarding_warning:
                log(
                    f"Waiting for onboarding secret {self.conductor_namespace}/{self.onboarding_secret_name}"
                )
                self.pending_onboarding_warning = True
            self.set_status(onboardingSecretPresent=False)
            self.close_connection()
            return

        self.pending_onboarding_warning = False
        self.set_status(onboardingSecretPresent=True)
        fingerprint, bundle, username, password = self.decode_onboarding_secret(secret)
        if (
            fingerprint == self.current_onboarding_fingerprint
            and self.connection is not None
            and self.connection.is_open
        ):
            return

        self.close_connection()
        self.connect(bundle, username, password)
        self.current_onboarding_fingerprint = fingerprint

    def refresh_trust_source(self, force=False):
        if not self.trust_source_required:
            return

        now = time.time()
        if not force and now - self.last_trust_source_poll < self.trust_poll_interval:
            return
        self.last_trust_source_poll = now

        if not os.path.isfile(self.trust_source_ca_path) or not os.path.isfile(self.trust_source_crl_path):
            if not self.pending_trust_source_warning:
                log(
                    "Waiting for local trust material at "
                    f"{self.trust_source_ca_path} and {self.trust_source_crl_path}"
                )
                self.pending_trust_source_warning = True
            self.set_status(trustSourcePublished=False)
            return

        ca_pem = "".join(pem_blocks(read_text_file(self.trust_source_ca_path), "CERTIFICATE"))
        crl_pem = "".join(pem_blocks(read_text_file(self.trust_source_crl_path), "X509 CRL"))
        if not ca_pem or not crl_pem:
            if not self.pending_trust_source_warning:
                log(
                    "Waiting for parseable local trust material at "
                    f"{self.trust_source_ca_path} and {self.trust_source_crl_path}"
                )
                self.pending_trust_source_warning = True
            self.set_status(trustSourcePublished=False)
            return

        self.pending_trust_source_warning = False
        metadata = {
            "participant": self.pod_name,
            "role": self.participant_role,
            "namespace": self.pod_namespace,
            "segment": self.segment_name,
            "caBlockCount": len(pem_blocks(ca_pem, "CERTIFICATE")),
            "crlBlockCount": len(pem_blocks(crl_pem, "X509 CRL")),
            "caSha256": sha256_text(ca_pem),
            "crlSha256": sha256_text(crl_pem),
        }
        fingerprint = sha256_text("\n".join([ca_pem, crl_pem, json.dumps(metadata, sort_keys=True)]))
        updated = self.k8s.upsert_secret(
            self.trust_source_secret_name,
            labels={
                CONDUCTOR_LABEL_KEY: TRUST_SOURCE_LABEL_VALUE,
                "pe-k8s.puppet.com/fabric-segment": sanitize_fragment(self.segment_name),
                "pe-k8s.puppet.com/participant": sanitize_fragment(self.pod_name),
            },
            annotations={
                "pe-k8s.puppet.com/participant-role": self.participant_role,
            },
            string_data={
                "ca.pem": ca_pem,
                "crl.pem": crl_pem,
                "metadata.json": json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            },
            namespace=self.conductor_namespace,
        )
        if updated or fingerprint != self.current_trust_source_fingerprint:
            log(f"Published trust source {self.conductor_namespace}/{self.trust_source_secret_name}")
        self.current_trust_source_fingerprint = fingerprint
        self.set_status(trustSourcePublished=True, lastTrustSourcePublishedAt=int(time.time()))

    def refresh_trust_bundle(self, force=False):
        if not self.trust_output_dir:
            return

        now = time.time()
        if not force and now - self.last_trust_bundle_poll < self.trust_poll_interval:
            return
        self.last_trust_bundle_poll = now

        secret = self.k8s.get_secret(self.trust_bundle_secret_name, namespace=self.conductor_namespace)
        if secret is None:
            if not self.pending_trust_bundle_warning:
                log(
                    f"Waiting for trust bundle secret {self.conductor_namespace}/{self.trust_bundle_secret_name}"
                )
                self.pending_trust_bundle_warning = True
            self.set_status(
                trustBundleAvailable=False,
                trustBundleInstalled=False,
                trustBundleSourceCount=0,
            )
            return

        data = secret.get("data", {})
        ca_pem = b64decode_text(data["ca.pem"]) if "ca.pem" in data else ""
        crl_pem = b64decode_text(data["crl.pem"]) if "crl.pem" in data else ""
        metadata_json = b64decode_text(data["metadata.json"]) if "metadata.json" in data else "{}\n"
        metadata = json.loads(metadata_json)
        if not ca_pem or not crl_pem:
            if not self.pending_trust_bundle_warning:
                log(
                    f"Waiting for populated trust bundle secret {self.conductor_namespace}/{self.trust_bundle_secret_name}"
                )
                self.pending_trust_bundle_warning = True
            self.set_status(
                trustBundleAvailable=False,
                trustBundleInstalled=False,
                trustBundleSourceCount=0,
            )
            return

        self.pending_trust_bundle_warning = False
        self.set_status(
            trustBundleAvailable=True,
            trustBundleSourceCount=int(metadata.get("sourceCount") or 0),
        )
        fingerprint = sha256_text("\n".join([ca_pem, crl_pem, metadata_json]))
        if fingerprint == self.current_trust_bundle_fingerprint:
            return

        write_text_file(os.path.join(self.trust_output_dir, "ca.pem"), ca_pem)
        write_text_file(os.path.join(self.trust_output_dir, "crl.pem"), crl_pem)
        write_text_file(os.path.join(self.trust_output_dir, "metadata.json"), metadata_json)
        self.current_trust_bundle_fingerprint = fingerprint
        self.set_status(trustBundleInstalled=True, lastTrustBundleInstalledAt=int(time.time()))
        log(f"Installed trust bundle {self.conductor_namespace}/{self.trust_bundle_secret_name}")

    def publish_heartbeat(self):
        if self.connection is None or self.channel is None or self.bundle is None:
            return
        now = time.time()
        if now < self.next_heartbeat:
            return
        payload = {
            "participant": self.bundle["participant"]["name"],
            "role": self.bundle["participant"].get("role", ""),
            "namespace": self.pod_namespace,
            "segment": self.bundle["segment"]["name"],
            "timestamp": int(now),
            "topology": self.bundle.get("topology", {}),
        }
        routing_key = f"participant.{sanitize_fragment(self.bundle['participant']['name'])}.heartbeat"
        self.channel.basic_publish(
            exchange=self.bundle["hub"]["exchanges"]["data"],
            routing_key=routing_key,
            body=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )
        self.next_heartbeat = now + self.heartbeat_interval

    def run(self):
        while True:
            try:
                self.refresh_trust_source(force=self.current_trust_source_fingerprint == "")
                self.refresh_trust_bundle(force=self.current_trust_bundle_fingerprint == "")
                self.refresh_onboarding(force=self.connection is None)
                if self.connection is not None and self.connection.is_open:
                    self.publish_heartbeat()
                    self.connection.process_data_events(time_limit=1)
                else:
                    time.sleep(1)
                self.set_status(lastError="")
            except KeyboardInterrupt:
                raise
            except Exception as error:
                self.set_status(lastError=str(error))
                log(f"Loop failed: {error}")
                self.close_connection()
                time.sleep(5)
            finally:
                self.write_status()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        mode = sys.argv[2] if len(sys.argv) > 2 else "ready"
        sys.exit(probe(mode))

    runtime = ParticipantRuntime()
    runtime.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
