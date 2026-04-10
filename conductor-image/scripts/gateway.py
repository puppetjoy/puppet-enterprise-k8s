#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import re
import select
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

import pika


DEFAULT_STATUS_FILENAME = "gateway-status.json"


def log(message):
    print(f"[gateway] {message}", flush=True)


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def env_bool(name, default=False):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


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


def http_request_raw(method, url, headers=None, data=None, context=None, timeout=30):
    request = urllib.request.Request(url, method=method, data=data)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, body
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        return error.code, body


def default_status_path():
    output_dir = os.environ.get("CONDUCTOR_GATEWAY_OUTPUT_DIR", "").strip() or "/tmp"
    return os.environ.get("CONDUCTOR_GATEWAY_STATUS_PATH", "").strip() or os.path.join(
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
    max_age_seconds = env_int("CONDUCTOR_GATEWAY_STATUS_MAX_AGE_SECONDS", 60)

    if not os.path.isfile(status_path):
        return 1

    try:
        status = read_json_file(status_path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
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
    if status.get("pcpProxyEnabled", False):
        if not status.get("pcpProxyReady", False):
            return 1
        if not status.get("localPcpBrokerHealthy", False):
            return 1
    if status.get("orchestrationProxyEnabled", False):
        if not status.get("orchestrationProxyReady", False):
            return 1
        if not status.get("localOrchestrationHealthy", False):
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

    def request(self, method, path, expected=None):
        status, body = http_request_raw(
            method,
            f"{self.base_url}{path}",
            headers=self.headers,
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


class TcpProxy:
    def __init__(self, name, listen_host, listen_port, target_host, target_port, connect_timeout_seconds=5):
        self.name = name
        self.listen_host = listen_host
        self.listen_port = int(listen_port)
        self.target_host = target_host
        self.target_port = int(target_port)
        self.connect_timeout_seconds = int(connect_timeout_seconds)
        self.lock = threading.RLock()
        self.server_socket = None
        self.server_thread = None
        self.running = False
        self.start_error = ""
        self.active_connections = 0

    def snapshot(self):
        with self.lock:
            thread_alive = self.server_thread is not None and self.server_thread.is_alive()
            return {
                "ready": bool(self.running and self.server_socket is not None and thread_alive),
                "startError": self.start_error,
                "activeConnections": self.active_connections,
            }

    def increment_connections(self):
        with self.lock:
            self.active_connections += 1

    def decrement_connections(self):
        with self.lock:
            self.active_connections = max(0, self.active_connections - 1)

    def ensure_running(self):
        with self.lock:
            if self.running and self.server_thread is not None and self.server_thread.is_alive():
                return

            self.running = False
            self.server_socket = None
            self.server_thread = None

            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.listen_host, self.listen_port))
            server.listen(128)
            server.settimeout(1.0)

            self.server_socket = server
            self.running = True
            self.start_error = ""
            self.server_thread = threading.Thread(
                target=self.serve_forever,
                name=f"conductor-gateway-{self.name}",
                daemon=True,
            )
            self.server_thread.start()
            log(
                f"Started {self.name} proxy on {self.listen_host}:{self.listen_port} "
                f"to {self.target_host}:{self.target_port}"
            )

    def serve_forever(self):
        while True:
            with self.lock:
                if not self.running or self.server_socket is None:
                    return
                server = self.server_socket
            try:
                client_socket, _ = server.accept()
            except socket.timeout:
                continue
            except OSError as error:
                with self.lock:
                    self.start_error = str(error)
                    self.running = False
                return

            thread = threading.Thread(
                target=self.handle_client,
                args=(client_socket,),
                name=f"conductor-gateway-{self.name}-conn",
                daemon=True,
            )
            thread.start()

    def handle_client(self, client_socket):
        upstream_socket = None
        self.increment_connections()
        try:
            upstream_socket = socket.create_connection(
                (self.target_host, self.target_port),
                timeout=self.connect_timeout_seconds,
            )
            client_socket.setblocking(False)
            upstream_socket.setblocking(False)
            self.bridge_bidirectional(client_socket, upstream_socket)
        except Exception:
            pass
        finally:
            for current in [client_socket, upstream_socket]:
                if current is None:
                    continue
                try:
                    current.close()
                except OSError:
                    pass
            self.decrement_connections()

    @staticmethod
    def bridge_bidirectional(client_socket, upstream_socket):
        sockets = [client_socket, upstream_socket]
        peer = {
            client_socket: upstream_socket,
            upstream_socket: client_socket,
        }
        closed = set()
        while len(closed) < 2:
            readable, _, exceptional = select.select(sockets, [], sockets, 1.0)
            if exceptional:
                return
            if not readable:
                continue
            for source in readable:
                if source in closed:
                    continue
                try:
                    chunk = source.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    return
                if not chunk:
                    closed.add(source)
                    try:
                        peer[source].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                try:
                    peer[source].sendall(chunk)
                except OSError:
                    return


class GatewayRuntime:
    def __init__(self):
        self.lock = threading.RLock()
        self.pod_name = os.environ["CONDUCTOR_POD_NAME"].strip()
        self.pod_namespace = os.environ["CONDUCTOR_POD_NAMESPACE"].strip()
        self.conductor_namespace = os.environ.get("CONDUCTOR_NAMESPACE", self.pod_namespace).strip() or self.pod_namespace
        self.resource_prefix = os.environ["CONDUCTOR_RESOURCE_PREFIX"].strip()
        self.segment_name = os.environ["CONDUCTOR_SEGMENT_NAME"].strip()
        self.gateway_role = os.environ.get("CONDUCTOR_GATEWAY_ROLE", "control-plane").strip() or "control-plane"
        self.secret_poll_interval = env_int("CONDUCTOR_GATEWAY_SECRET_POLL_INTERVAL_SECONDS", 15)
        self.publish_interval = env_int("CONDUCTOR_GATEWAY_PUBLISH_INTERVAL_SECONDS", 15)
        self.peer_status_max_age = env_int("CONDUCTOR_GATEWAY_PEER_STATUS_MAX_AGE_SECONDS", 60)
        self.participant_status_path = (
            os.environ.get("CONDUCTOR_GATEWAY_PARTICIPANT_STATUS_PATH", "").strip()
            or "/tmp/participant-status.json"
        )
        self.participant_status_max_age = env_int(
            "CONDUCTOR_GATEWAY_PARTICIPANT_STATUS_MAX_AGE_SECONDS",
            60,
        )
        self.output_dir = os.environ.get("CONDUCTOR_GATEWAY_OUTPUT_DIR", "").strip() or "/tmp"
        self.status_path = default_status_path()
        self.peer_dir = os.path.join(self.output_dir, "peers")

        self.pcp_proxy_enabled = env_bool("CONDUCTOR_GATEWAY_PCP_PROXY_ENABLED", True)
        self.pcp_listen_host = os.environ.get("CONDUCTOR_GATEWAY_PCP_LISTEN_HOST", "").strip() or "0.0.0.0"
        self.pcp_listen_port = env_int("CONDUCTOR_GATEWAY_PCP_LISTEN_PORT", 18142)
        self.pcp_target_host = os.environ.get("CONDUCTOR_GATEWAY_PCP_TARGET_HOST", "").strip() or "127.0.0.1"
        self.pcp_target_port = env_int("CONDUCTOR_GATEWAY_PCP_TARGET_PORT", 8142)
        self.pcp_connect_timeout_seconds = env_int("CONDUCTOR_GATEWAY_PCP_CONNECT_TIMEOUT_SECONDS", 5)
        self.pcp_healthcheck_timeout_seconds = env_int("CONDUCTOR_GATEWAY_PCP_HEALTHCHECK_TIMEOUT_SECONDS", 5)

        self.orchestration_proxy_enabled = env_bool("CONDUCTOR_GATEWAY_ORCHESTRATION_PROXY_ENABLED", True)
        self.orchestration_listen_host = (
            os.environ.get("CONDUCTOR_GATEWAY_ORCHESTRATION_LISTEN_HOST", "").strip() or "0.0.0.0"
        )
        self.orchestration_listen_port = env_int("CONDUCTOR_GATEWAY_ORCHESTRATION_LISTEN_PORT", 18143)
        self.orchestration_target_host = (
            os.environ.get("CONDUCTOR_GATEWAY_ORCHESTRATION_TARGET_HOST", "").strip() or "127.0.0.1"
        )
        self.orchestration_target_port = env_int("CONDUCTOR_GATEWAY_ORCHESTRATION_TARGET_PORT", 8143)
        self.orchestration_connect_timeout_seconds = env_int(
            "CONDUCTOR_GATEWAY_ORCHESTRATION_CONNECT_TIMEOUT_SECONDS",
            5,
        )
        self.orchestration_status_url = (
            os.environ.get("CONDUCTOR_GATEWAY_ORCHESTRATION_STATUS_URL", "").strip()
            or f"https://{self.orchestration_target_host}:{self.orchestration_target_port}/status/v1/services/status-service"
        )
        self.orchestration_status_timeout_seconds = env_int(
            "CONDUCTOR_GATEWAY_ORCHESTRATION_STATUS_TIMEOUT_SECONDS",
            5,
        )

        self.k8s = K8sApi(self.pod_namespace)
        self.onboarding_secret_name = participant_secret_name(
            self.resource_prefix,
            self.segment_name,
            self.pod_name,
            "onboarding",
        )
        self.queue_name = f"gateway.{sanitize_fragment(self.pod_name)}"
        self.current_onboarding_fingerprint = ""
        self.last_secret_poll = 0
        self.pending_onboarding_warning = False
        self.next_publish = 0

        self.connection = None
        self.channel = None
        self.bundle = None
        self.publish_username = ""
        self.publish_password = ""

        self.pcp_proxy = None
        if self.pcp_proxy_enabled:
            self.pcp_proxy = TcpProxy(
                "pcp",
                self.pcp_listen_host,
                self.pcp_listen_port,
                self.pcp_target_host,
                self.pcp_target_port,
                connect_timeout_seconds=self.pcp_connect_timeout_seconds,
            )

        self.orchestration_proxy = None
        if self.orchestration_proxy_enabled:
            self.orchestration_proxy = TcpProxy(
                "orchestration",
                self.orchestration_listen_host,
                self.orchestration_listen_port,
                self.orchestration_target_host,
                self.orchestration_target_port,
                connect_timeout_seconds=self.orchestration_connect_timeout_seconds,
            )

        self.status = {
            "participant": self.pod_name,
            "namespace": self.pod_namespace,
            "segment": self.segment_name,
            "role": self.gateway_role,
            "onboardingSecretName": self.onboarding_secret_name,
            "queueName": self.queue_name,
            "connected": False,
            "onboardingSecretPresent": False,
            "participantReady": False,
            "participantStatusAgeSeconds": None,
            "pcpProxyEnabled": self.pcp_proxy_enabled,
            "pcpProxyReady": not self.pcp_proxy_enabled,
            "pcpProxyListenAddress": f"{self.pcp_listen_host}:{self.pcp_listen_port}" if self.pcp_proxy_enabled else "",
            "pcpProxyTargetAddress": f"{self.pcp_target_host}:{self.pcp_target_port}" if self.pcp_proxy_enabled else "",
            "pcpProxyActiveConnections": 0,
            "pcpProxyStartError": "",
            "localPcpBrokerHealthy": not self.pcp_proxy_enabled,
            "localPcpLastError": "",
            "orchestrationProxyEnabled": self.orchestration_proxy_enabled,
            "orchestrationProxyReady": not self.orchestration_proxy_enabled,
            "orchestrationProxyListenAddress": (
                f"{self.orchestration_listen_host}:{self.orchestration_listen_port}"
                if self.orchestration_proxy_enabled else ""
            ),
            "orchestrationProxyTargetAddress": (
                f"{self.orchestration_target_host}:{self.orchestration_target_port}"
                if self.orchestration_proxy_enabled else ""
            ),
            "orchestrationProxyActiveConnections": 0,
            "orchestrationProxyStartError": "",
            "localOrchestrationHealthy": not self.orchestration_proxy_enabled,
            "localOrchestrationStatusUrl": self.orchestration_status_url if self.orchestration_proxy_enabled else "",
            "localOrchestrationLastError": "",
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
        with self.lock:
            self.status.update(updates)

    def snapshot_status(self):
        with self.lock:
            return dict(self.status)

    def write_status(self):
        status = self.snapshot_status()
        status["lastUpdatedAt"] = int(time.time())
        with self.lock:
            self.status["lastUpdatedAt"] = status["lastUpdatedAt"]
        write_text_file(self.status_path, json.dumps(status, indent=2, sort_keys=True) + "\n")

    def close_connection(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None
        self.channel = None
        with self.lock:
            self.bundle = None
            self.publish_username = ""
            self.publish_password = ""
        self.set_status(connected=False)

    def peer_status_path(self, participant_name):
        filename = f"{sanitize_fragment(participant_name)}.json"
        return os.path.join(self.peer_dir, filename)

    def ensure_proxies_running(self):
        pcp_snapshot = {"ready": True, "startError": "", "activeConnections": 0}
        if self.pcp_proxy is not None:
            self.pcp_proxy.ensure_running()
            pcp_snapshot = self.pcp_proxy.snapshot()
        self.set_status(
            pcpProxyReady=pcp_snapshot["ready"],
            pcpProxyStartError=pcp_snapshot["startError"],
            pcpProxyActiveConnections=pcp_snapshot["activeConnections"],
        )

        orchestration_snapshot = {"ready": True, "startError": "", "activeConnections": 0}
        if self.orchestration_proxy is not None:
            self.orchestration_proxy.ensure_running()
            orchestration_snapshot = self.orchestration_proxy.snapshot()
        self.set_status(
            orchestrationProxyReady=orchestration_snapshot["ready"],
            orchestrationProxyStartError=orchestration_snapshot["startError"],
            orchestrationProxyActiveConnections=orchestration_snapshot["activeConnections"],
        )

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

    def refresh_local_pcp_status(self):
        if not self.pcp_proxy_enabled:
            self.set_status(localPcpBrokerHealthy=True, localPcpLastError="")
            return

        try:
            connection = socket.create_connection(
                (self.pcp_target_host, self.pcp_target_port),
                timeout=self.pcp_healthcheck_timeout_seconds,
            )
            connection.close()
            self.set_status(localPcpBrokerHealthy=True, localPcpLastError="")
        except Exception as error:
            self.set_status(localPcpBrokerHealthy=False, localPcpLastError=str(error))
            raise

    def refresh_local_orchestration_status(self):
        if not self.orchestration_proxy_enabled:
            self.set_status(localOrchestrationHealthy=True, localOrchestrationLastError="")
            return

        try:
            context = ssl._create_unverified_context()
            status_code, _body = http_request_raw(
                "GET",
                self.orchestration_status_url,
                context=context,
                timeout=self.orchestration_status_timeout_seconds,
            )
            if status_code != 200:
                raise RuntimeError(f"orchestration status endpoint returned {status_code}")
            self.set_status(localOrchestrationHealthy=True, localOrchestrationLastError="")
        except Exception as error:
            self.set_status(localOrchestrationHealthy=False, localOrchestrationLastError=str(error))
            raise

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
            if now - last_updated_at > self.peer_status_max_age:
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

        if payload.get("kind") != "ConductorGatewayStatus":
            channel.basic_ack(method.delivery_tag)
            return

        status = payload.get("status") or {}
        participant_name = (status.get("participant") or "").strip()
        if participant_name and participant_name != self.pod_name:
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
                "connection_name": f"{self.pod_namespace}/{self.pod_name}/gateway",
            },
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()

        data_exchange = bundle["hub"]["exchanges"]["data"]
        channel.exchange_declare(exchange=data_exchange, exchange_type="topic", durable=True)
        channel.queue_declare(queue=self.queue_name, durable=True)
        channel.queue_bind(exchange=data_exchange, queue=self.queue_name, routing_key="gateway.#")
        channel.basic_consume(queue=self.queue_name, on_message_callback=self.on_message)

        self.connection = connection
        self.channel = channel
        with self.lock:
            self.bundle = bundle
            self.publish_username = username
            self.publish_password = password
        self.next_publish = 0
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

    def publish_status(self):
        if self.connection is None or self.channel is None or self.bundle is None:
            return
        now = int(time.time())
        if now < self.next_publish:
            return

        self.refresh_peer_counts()
        if self.pcp_proxy is not None:
            self.set_status(pcpProxyActiveConnections=self.pcp_proxy.snapshot()["activeConnections"])
        if self.orchestration_proxy is not None:
            self.set_status(
                orchestrationProxyActiveConnections=self.orchestration_proxy.snapshot()["activeConnections"]
            )
        self.set_status(lastPublishedAt=now)
        payload_status = self.snapshot_status()
        payload_status["lastUpdatedAt"] = now
        payload = {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorGatewayStatus",
            "publishedAt": now,
            "status": payload_status,
        }
        routing_key = f"gateway.{sanitize_fragment(self.pod_name)}.status"
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
                self.ensure_proxies_running()
                self.refresh_participant_status()
                self.refresh_local_pcp_status()
                self.refresh_local_orchestration_status()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                log(f"Local gateway health refresh failed: {error}")

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

    runtime = GatewayRuntime()
    runtime.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
