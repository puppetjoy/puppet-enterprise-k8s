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
import urllib.request
from datetime import datetime, timezone

import pika


DEFAULT_STATUS_FILENAME = "relay-status.json"


def log(message):
    print(f"[relay] {message}", flush=True)


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def sanitize_fragment(value):
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "default"


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


def read_json_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_text_file(path, content):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temp_path, path)


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
    output_dir = os.environ.get("CONDUCTOR_RELAY_OUTPUT_DIR", "").strip() or "/tmp"
    return os.environ.get("CONDUCTOR_RELAY_STATUS_PATH", "").strip() or os.path.join(
        output_dir,
        DEFAULT_STATUS_FILENAME,
    )


def participant_status_ready(status, max_age_seconds):
    last_updated_at = int(status.get("lastUpdatedAt") or 0)
    if last_updated_at <= 0:
        return False, 0

    age_seconds = int(time.time()) - last_updated_at
    if age_seconds > max_age_seconds:
        return False, age_seconds
    if not status.get("onboardingSecretPresent", False):
        return False, age_seconds
    if not status.get("connected", False):
        return False, age_seconds
    if not status.get("trustBundleAvailable", False):
        return False, age_seconds
    if not status.get("trustBundleInstalled", False):
        return False, age_seconds
    if int(status.get("trustBundleSourceCount") or 0) < 1:
        return False, age_seconds
    if status.get("trustSourceRequired", False) and not status.get("trustSourcePublished", False):
        return False, age_seconds
    return True, age_seconds


def probe(mode):
    status_path = default_status_path()
    max_age_seconds = env_int("CONDUCTOR_RELAY_STATUS_MAX_AGE_SECONDS", 60)

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

    if not status.get("connected", False):
        return 1
    if not status.get("participantReady", False):
        return 1
    if not status.get("localPuppetdbHealthy", False):
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


class RelayRuntime:
    def __init__(self):
        self.pod_name = os.environ["CONDUCTOR_POD_NAME"].strip()
        self.pod_namespace = os.environ["CONDUCTOR_POD_NAMESPACE"].strip()
        self.conductor_namespace = os.environ.get("CONDUCTOR_NAMESPACE", self.pod_namespace).strip() or self.pod_namespace
        self.resource_prefix = os.environ["CONDUCTOR_RESOURCE_PREFIX"].strip()
        self.segment_name = os.environ["CONDUCTOR_SEGMENT_NAME"].strip()
        self.relay_role = os.environ.get("CONDUCTOR_RELAY_ROLE", "worker").strip() or "worker"
        self.secret_poll_interval = env_int("CONDUCTOR_RELAY_SECRET_POLL_INTERVAL_SECONDS", 15)
        self.publish_interval = env_int("CONDUCTOR_RELAY_PUBLISH_INTERVAL_SECONDS", 15)
        self.peer_status_max_age = env_int("CONDUCTOR_RELAY_PEER_STATUS_MAX_AGE_SECONDS", 60)
        self.puppetdb_sync_max_age = env_int("CONDUCTOR_RELAY_PUPPETDB_SYNC_MAX_AGE_SECONDS", 900)
        self.participant_status_max_age = env_int(
            "CONDUCTOR_RELAY_PARTICIPANT_STATUS_MAX_AGE_SECONDS",
            60,
        )
        self.puppetdb_status_url = (
            os.environ.get("CONDUCTOR_RELAY_PUPPETDB_STATUS_URL", "").strip()
            or "https://127.0.0.1:8081/status/v1/services?level=debug"
        )
        self.output_dir = os.environ.get("CONDUCTOR_RELAY_OUTPUT_DIR", "").strip() or "/tmp"
        self.status_path = default_status_path()
        self.peer_dir = os.path.join(self.output_dir, "peers")
        self.participant_status_path = (
            os.environ.get("CONDUCTOR_RELAY_PARTICIPANT_STATUS_PATH", "").strip()
            or "/tmp/participant-status.json"
        )

        self.k8s = K8sApi(self.pod_namespace)
        self.onboarding_secret_name = participant_secret_name(
            self.resource_prefix,
            self.segment_name,
            self.pod_name,
            "onboarding",
        )
        self.queue_name = f"relay.{sanitize_fragment(self.pod_name)}"

        self.current_onboarding_fingerprint = ""
        self.last_secret_poll = 0
        self.pending_onboarding_warning = False

        self.connection = None
        self.channel = None
        self.bundle = None
        self.next_publish = 0

        self.status = {
            "participant": self.pod_name,
            "namespace": self.pod_namespace,
            "segment": self.segment_name,
            "role": self.relay_role,
            "onboardingSecretName": self.onboarding_secret_name,
            "queueName": self.queue_name,
            "connected": False,
            "participantReady": False,
            "participantStatusAgeSeconds": None,
            "localPuppetdbHealthy": False,
            "localReadDbUp": False,
            "localWriteDbUp": False,
            "localSyncState": "",
            "localLastSuccessfulSyncAt": "",
            "localSyncAgeSeconds": None,
            "peerStatusCount": 0,
            "peerControlPlaneCount": 0,
            "peerCompilerCount": 0,
            "lastPublishedAt": 0,
            "lastReceivedAt": 0,
            "lastConnectedAt": 0,
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

    def peer_status_path(self, participant_name):
        filename = f"{sanitize_fragment(participant_name)}.json"
        return os.path.join(self.peer_dir, filename)

    def refresh_participant_status(self):
        if not os.path.isfile(self.participant_status_path):
            self.set_status(participantReady=False, participantStatusAgeSeconds=None)
            return

        status = read_json_file(self.participant_status_path)
        ready, age_seconds = participant_status_ready(status, self.participant_status_max_age)
        self.set_status(
            participantReady=ready,
            participantStatusAgeSeconds=age_seconds,
        )

    def parse_sync_age_seconds(self, value):
        if not value:
            return None
        last_sync_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - last_sync_time).total_seconds())

    def refresh_local_puppetdb_status(self):
        context = ssl._create_unverified_context()
        status_code, body = http_request("GET", self.puppetdb_status_url, context=context)
        if status_code != 200:
            raise RuntimeError(f"PuppetDB status endpoint returned {status_code}")

        payload = json.loads(body)
        puppetdb_status = payload.get("puppetdb-status") or {}
        if puppetdb_status.get("state") != "running":
            raise RuntimeError("PuppetDB status service is not running")

        details = puppetdb_status.get("status") or {}
        sync_status = details.get("sync_status") or {}
        sync_state = (sync_status.get("state") or "").strip()
        last_successful_sync = (sync_status.get("last_successful_sync") or "").strip()
        sync_age_seconds = self.parse_sync_age_seconds(last_successful_sync)
        read_db_up = bool(details.get("read_db_up?"))
        write_db_up = bool(details.get("write_db_up?"))

        healthy = read_db_up and write_db_up
        if self.relay_role == "compiler":
            if sync_state not in {"idle", "syncing", "error"}:
                healthy = False
            if not last_successful_sync or sync_age_seconds is None:
                healthy = False
            elif sync_age_seconds > self.puppetdb_sync_max_age:
                healthy = False
        elif sync_state and sync_state not in {"idle", "syncing", "error"}:
            healthy = False

        self.set_status(
            localPuppetdbHealthy=healthy,
            localReadDbUp=read_db_up,
            localWriteDbUp=write_db_up,
            localSyncState=sync_state,
            localLastSuccessfulSyncAt=last_successful_sync,
            localSyncAgeSeconds=sync_age_seconds,
        )

    def refresh_peer_counts(self):
        fresh_statuses = []
        os.makedirs(self.peer_dir, exist_ok=True)
        now = int(time.time())
        for entry in os.scandir(self.peer_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                status = read_json_file(entry.path)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            last_updated_at = int(status.get("lastUpdatedAt") or 0)
            if last_updated_at <= 0:
                continue
            age_seconds = now - last_updated_at
            if age_seconds > self.peer_status_max_age:
                continue
            fresh_statuses.append(status)

        self.set_status(
            peerStatusCount=len(fresh_statuses),
            peerControlPlaneCount=sum(1 for status in fresh_statuses if status.get("role") == "control-plane"),
            peerCompilerCount=sum(1 for status in fresh_statuses if status.get("role") == "compiler"),
        )

    def on_message(self, channel, method, _properties, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            channel.basic_ack(method.delivery_tag)
            return

        if payload.get("kind") != "ConductorRelayStatus":
            channel.basic_ack(method.delivery_tag)
            return

        status = payload.get("status") or {}
        participant_name = (status.get("participant") or "").strip()
        if not participant_name or participant_name == self.pod_name:
            channel.basic_ack(method.delivery_tag)
            return

        write_text_file(
            self.peer_status_path(participant_name),
            json.dumps(status, indent=2, sort_keys=True) + "\n",
        )
        self.set_status(lastReceivedAt=int(time.time()))
        channel.basic_ack(method.delivery_tag)

    def connect(self, bundle, username, password):
        credentials = pika.PlainCredentials(username, password)
        parameters = pika.ConnectionParameters(
            host=bundle["hub"]["host"],
            port=int(bundle["hub"]["amqpPort"]),
            virtual_host=bundle["hub"]["vhost"],
            credentials=credentials,
            heartbeat=max(self.publish_interval * 2, 30),
            blocked_connection_timeout=30,
            client_properties={
                "connection_name": f"{self.pod_namespace}/{self.pod_name}/relay",
            },
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()

        data_exchange = bundle["hub"]["exchanges"]["data"]
        channel.exchange_declare(exchange=data_exchange, exchange_type="topic", durable=True)
        channel.queue_declare(queue=self.queue_name, durable=True)
        channel.queue_bind(exchange=data_exchange, queue=self.queue_name, routing_key="relay.#")
        channel.basic_consume(queue=self.queue_name, on_message_callback=self.on_message)

        self.connection = connection
        self.channel = channel
        self.bundle = bundle
        self.next_publish = 0
        self.set_status(connected=True, lastConnectedAt=int(time.time()))
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
            self.close_connection()
            return

        self.pending_onboarding_warning = False
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

    def publish_status(self):
        if self.connection is None or self.channel is None or self.bundle is None:
            return
        now = int(time.time())
        if now < self.next_publish:
            return

        self.refresh_peer_counts()
        self.set_status(lastPublishedAt=now)
        payload_status = dict(self.status)
        payload_status["lastUpdatedAt"] = now
        payload = {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorRelayStatus",
            "publishedAt": now,
            "status": payload_status,
        }
        routing_key = f"relay.{sanitize_fragment(self.pod_name)}.status"
        self.channel.basic_publish(
            exchange=self.bundle["hub"]["exchanges"]["data"],
            routing_key=routing_key,
            body=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )
        self.next_publish = now + self.publish_interval

    def run(self):
        while True:
            last_error = ""
            try:
                self.refresh_participant_status()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                self.set_status(participantReady=False, participantStatusAgeSeconds=None)
                log(f"Participant status refresh failed: {error}")

            try:
                self.refresh_local_puppetdb_status()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                self.set_status(
                    localPuppetdbHealthy=False,
                    localReadDbUp=False,
                    localWriteDbUp=False,
                    localSyncState="",
                    localLastSuccessfulSyncAt="",
                    localSyncAgeSeconds=None,
                )
                log(f"PuppetDB status refresh failed: {error}")

            try:
                self.refresh_onboarding(force=self.connection is None or not self.connection.is_open)
                if self.connection is not None and self.connection.is_open:
                    self.publish_status()
                    self.connection.process_data_events(time_limit=1)
                else:
                    time.sleep(1)
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                log(f"Fabric loop failed: {error}")
                self.close_connection()
                time.sleep(5)

            self.refresh_peer_counts()
            self.set_status(lastError=last_error)
            self.write_status()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        mode = sys.argv[2] if len(sys.argv) > 2 else "ready"
        sys.exit(probe(mode))

    runtime = RelayRuntime()
    runtime.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
