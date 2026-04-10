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


def b64decode_text(value):
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


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


class ParticipantRuntime:
    def __init__(self):
        self.pod_name = os.environ["CONDUCTOR_POD_NAME"].strip()
        self.pod_namespace = os.environ["CONDUCTOR_POD_NAMESPACE"].strip()
        self.conductor_namespace = os.environ.get("CONDUCTOR_NAMESPACE", self.pod_namespace).strip() or self.pod_namespace
        self.resource_prefix = os.environ["CONDUCTOR_RESOURCE_PREFIX"].strip()
        self.segment_name = os.environ["CONDUCTOR_SEGMENT_NAME"].strip()
        self.secret_poll_interval = env_int("CONDUCTOR_SECRET_POLL_INTERVAL_SECONDS", 15)
        self.heartbeat_interval = env_int("CONDUCTOR_HEARTBEAT_INTERVAL_SECONDS", 15)
        self.k8s = K8sApi(self.pod_namespace)
        self.current_fingerprint = ""
        self.secret_name = self.build_secret_name(self.resource_prefix, self.segment_name, self.pod_name)
        self.connection = None
        self.channel = None
        self.bundle = None
        self.last_secret_poll = 0
        self.next_heartbeat = 0
        self.pending_secret_warning = False

    @staticmethod
    def build_secret_name(prefix, segment_name, participant_name):
        parts = [
            sanitize_fragment(prefix),
            sanitize_fragment(segment_name),
            sanitize_fragment(participant_name),
            "onboarding",
        ]
        name = "-".join(part for part in parts if part)
        return name[:63].rstrip("-")

    @staticmethod
    def decode_secret(secret):
        data = secret.get("data", {})
        onboarding_json = b64decode_text(data["onboarding.json"])
        username = b64decode_text(data["username"])
        password = b64decode_text(data["password"])
        fingerprint = hashlib.sha256(
            "\n".join([onboarding_json, username, password]).encode("utf-8")
        ).hexdigest()
        bundle = json.loads(onboarding_json)
        return fingerprint, bundle, username, password

    def close_connection(self):
        if self.connection is None:
            return
        try:
            self.connection.close()
        except Exception:
            pass
        self.connection = None
        self.channel = None
        self.bundle = None

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
        log(
            "Connected to Fabric as "
            f"{bundle['participant']['name']} on {bundle['segment']['name']} "
            f"via {bundle['hub']['host']}:{bundle['hub']['amqpPort']}"
        )

    def refresh_secret(self, force=False):
        now = time.time()
        if not force and now - self.last_secret_poll < self.secret_poll_interval:
            return
        self.last_secret_poll = now

        secret = self.k8s.get_secret(self.secret_name, namespace=self.conductor_namespace)
        if secret is None:
            if not self.pending_secret_warning:
                log(
                    f"Waiting for onboarding secret {self.conductor_namespace}/{self.secret_name}"
                )
                self.pending_secret_warning = True
            self.close_connection()
            return

        self.pending_secret_warning = False
        fingerprint, bundle, username, password = self.decode_secret(secret)
        if fingerprint == self.current_fingerprint and self.connection is not None and self.connection.is_open:
            return

        self.close_connection()
        self.connect(bundle, username, password)
        self.current_fingerprint = fingerprint

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
                self.refresh_secret(force=self.connection is None)
                if self.connection is not None and self.connection.is_open:
                    self.publish_heartbeat()
                    self.connection.process_data_events(time_limit=1)
                else:
                    time.sleep(1)
            except KeyboardInterrupt:
                raise
            except Exception as error:
                log(f"Loop failed: {error}")
                self.close_connection()
                time.sleep(5)


def main():
    runtime = ParticipantRuntime()
    runtime.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
