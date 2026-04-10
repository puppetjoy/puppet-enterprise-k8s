#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pika


DEFAULT_STATUS_FILENAME = "relay-status.json"
DEFAULT_REPLICATED_COMMANDS = [
    "replace facts",
    "store report",
    "deactivate node",
]
DEFAULT_TARGET_ROLES = [
    "control-plane",
]
DEFAULT_COMMAND_PROXY_PATH = "/pdb/cmd/v1"


def log(message):
    print(f"[relay] {message}", flush=True)


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


def env_csv(name, default=None):
    value = os.environ.get(name, "").strip()
    if not value:
        return list(default or [])
    items = []
    for entry in value.split(","):
        entry = entry.strip()
        if entry and entry not in items:
            items.append(entry)
    return items


def sanitize_fragment(value):
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "default"


def normalize_command_name(value):
    return re.sub(r"\s+", " ", value.replace("_", " ").replace("-", " ").strip().lower())


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


def http_request_raw(method, url, headers=None, data=None, context=None):
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


def http_request_json(method, url, headers=None, payload=None, context=None):
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return http_request_raw(method, url, headers=headers, data=data, context=context)


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
    if status.get("commandProxyEnabled", False) and not status.get("commandProxyReady", False):
        return 1

    return 0


class RelayLocalCommandError(RuntimeError):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class LocalCommandHandler(BaseHTTPRequestHandler):
    server_version = "ConductorRelay/0.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def text_response(self, status_code, message):
        body = (message.rstrip() + "\n").encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, status_code, payload):
        body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_request_body(self):
        transfer_encoding = self.headers.get("Transfer-Encoding", "").strip().lower()
        if "chunked" in transfer_encoding:
            chunks = []
            while True:
                line = self.rfile.readline()
                if not line:
                    raise RelayLocalCommandError(400, "unexpected EOF while reading chunked body")
                chunk_size_text = line.split(b";", 1)[0].strip()
                try:
                    chunk_size = int(chunk_size_text, 16)
                except ValueError as error:
                    raise RelayLocalCommandError(400, "invalid chunk size") from error
                if chunk_size == 0:
                    while True:
                        trailer_line = self.rfile.readline()
                        if not trailer_line or trailer_line in {b"\r\n", b"\n"}:
                            return b"".join(chunks)
                chunk = self.rfile.read(chunk_size)
                if len(chunk) != chunk_size:
                    raise RelayLocalCommandError(400, "unexpected EOF while reading chunk data")
                chunks.append(chunk)
                crlf = self.rfile.read(2)
                if crlf != b"\r\n":
                    raise RelayLocalCommandError(400, "invalid chunk terminator")

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise RelayLocalCommandError(400, "invalid content length") from error
        return self.rfile.read(content_length) if content_length > 0 else b""

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != self.server.runtime.command_proxy_path:
            self.text_response(404, "unknown relay endpoint")
            return

        try:
            body = self.read_request_body()
            payload = self.server.runtime.handle_local_command_submission(parsed, self.headers, body)
        except RelayLocalCommandError as error:
            self.text_response(error.status_code, error.message)
            return
        except Exception as error:  # pragma: no cover - defensive fallback
            self.text_response(500, str(error))
            return

        self.json_response(200, payload)


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
        status, body = http_request_json(
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
        self.lock = threading.RLock()
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
        self.local_puppetdb_command_url = (
            os.environ.get("CONDUCTOR_RELAY_LOCAL_PUPPETDB_COMMAND_URL", "").strip()
            or "https://127.0.0.1:8081/pdb/cmd/v1"
        )
        self.output_dir = os.environ.get("CONDUCTOR_RELAY_OUTPUT_DIR", "").strip() or "/tmp"
        self.status_path = default_status_path()
        self.peer_dir = os.path.join(self.output_dir, "peers")
        self.processed_dir = os.path.join(self.output_dir, "processed")
        self.participant_status_path = (
            os.environ.get("CONDUCTOR_RELAY_PARTICIPANT_STATUS_PATH", "").strip()
            or "/tmp/participant-status.json"
        )
        self.command_proxy_enabled = env_bool("CONDUCTOR_RELAY_COMMAND_PROXY_ENABLED", False)
        self.command_proxy_bind_host = (
            os.environ.get("CONDUCTOR_RELAY_COMMAND_PROXY_BIND_HOST", "").strip()
            or "0.0.0.0"
        )
        self.command_proxy_port = env_int("CONDUCTOR_RELAY_COMMAND_PROXY_PORT", 18081)
        self.command_proxy_path = (
            os.environ.get("CONDUCTOR_RELAY_COMMAND_PROXY_PATH", "").strip()
            or DEFAULT_COMMAND_PROXY_PATH
        )
        self.command_target_roles = env_csv(
            "CONDUCTOR_RELAY_COMMAND_TARGET_ROLES",
            default=DEFAULT_TARGET_ROLES,
        )
        self.replicated_commands = set(
            normalize_command_name(command)
            for command in env_csv(
                "CONDUCTOR_RELAY_REPLICATED_COMMANDS",
                default=DEFAULT_REPLICATED_COMMANDS,
            )
        )
        self.puppet_conf_path = (
            os.environ.get("CONDUCTOR_RELAY_PUPPET_CONF_PATH", "").strip()
            or "/etc/puppetlabs/puppet/puppet.conf"
        )
        self.puppet_ssl_dir = (
            os.environ.get("CONDUCTOR_RELAY_PUPPET_SSL_DIR", "").strip()
            or "/etc/puppetlabs/puppet/ssl"
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
        self.command_proxy_start_error = ""

        self.connection = None
        self.channel = None
        self.bundle = None
        self.publish_username = ""
        self.publish_password = ""
        self.next_publish = 0

        self.command_proxy_server = None
        self.command_proxy_thread = None

        self.status = {
            "participant": self.pod_name,
            "namespace": self.pod_namespace,
            "segment": self.segment_name,
            "role": self.relay_role,
            "onboardingSecretName": self.onboarding_secret_name,
            "queueName": self.queue_name,
            "connected": False,
            "onboardingSecretPresent": False,
            "participantReady": False,
            "participantStatusAgeSeconds": None,
            "localPuppetdbHealthy": False,
            "localReadDbUp": False,
            "localWriteDbUp": False,
            "localSyncState": "",
            "localLastSuccessfulSyncAt": "",
            "localSyncAgeSeconds": None,
            "commandProxyEnabled": self.command_proxy_enabled,
            "commandProxyReady": False,
            "commandProxySubmitUrl": "",
            "commandTargetRoles": list(self.command_target_roles),
            "replicatedCommands": sorted(self.replicated_commands),
            "localPublishedCommandCount": 0,
            "localSkippedCommandCount": 0,
            "replayedCommandCount": 0,
            "replayFailureCount": 0,
            "lastCommandPublishedAt": 0,
            "lastCommandReplayedAt": 0,
            "lastReplayError": "",
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

    def processed_message_path(self, message_id):
        return os.path.join(self.processed_dir, f"{sanitize_fragment(message_id)}.json")

    def parse_puppet_certname(self):
        if not os.path.isfile(self.puppet_conf_path):
            raise RuntimeError(f"puppet.conf not found at {self.puppet_conf_path}")
        with open(self.puppet_conf_path, "r", encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"^\s*certname\s*=\s*(\S+)\s*$", line)
                if match:
                    return match.group(1)
        raise RuntimeError(f"certname not found in {self.puppet_conf_path}")

    def puppet_ssl_paths(self):
        certname = self.parse_puppet_certname()
        cert_path = os.path.join(self.puppet_ssl_dir, "certs", f"{certname}.pem")
        key_path = os.path.join(self.puppet_ssl_dir, "private_keys", f"{certname}.pem")
        ca_path = os.path.join(self.puppet_ssl_dir, "certs", "ca.pem")
        for path in [cert_path, key_path, ca_path]:
            if not os.path.isfile(path):
                raise RuntimeError(f"required SSL file not found: {path}")
        return certname, cert_path, key_path, ca_path

    def command_proxy_submit_url(self):
        certname, _, _, _ = self.puppet_ssl_paths()
        return f"https://{certname}:{self.command_proxy_port}{self.command_proxy_path}"

    def build_local_puppetdb_context(self):
        _, cert_path, key_path, ca_path = self.puppet_ssl_paths()
        context = ssl.create_default_context(cafile=ca_path)
        context.check_hostname = False
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return context

    def ensure_command_proxy_running(self):
        if not self.command_proxy_enabled:
            return

        if (
            self.command_proxy_server is not None
            and self.command_proxy_thread is not None
            and self.command_proxy_thread.is_alive()
        ):
            self.set_status(commandProxyReady=True)
            return

        try:
            _, cert_path, key_path, _ = self.puppet_ssl_paths()
            server = ThreadingHTTPServer(
                (self.command_proxy_bind_host, self.command_proxy_port),
                LocalCommandHandler,
            )
            server.runtime = self
            server.daemon_threads = True
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=cert_path, keyfile=key_path)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(
                target=server.serve_forever,
                name="conductor-relay-command-proxy",
                daemon=True,
            )
            thread.start()
            self.command_proxy_server = server
            self.command_proxy_thread = thread
            self.command_proxy_start_error = ""
            self.set_status(
                commandProxyReady=True,
                commandProxySubmitUrl=self.command_proxy_submit_url(),
            )
            log(
                "Started command proxy on "
                f"{self.command_proxy_bind_host}:{self.command_proxy_port}"
            )
        except Exception as error:
            if str(error) != self.command_proxy_start_error:
                log(f"Command proxy startup failed: {error}")
                self.command_proxy_start_error = str(error)
            self.set_status(commandProxyReady=False)

    def current_publish_credentials(self):
        with self.lock:
            if self.bundle is None or not self.publish_username or not self.publish_password:
                raise RuntimeError("Relay is not connected to Fabric")
            return self.bundle, self.publish_username, self.publish_password

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
        status_code, body = http_request_raw("GET", self.puppetdb_status_url, context=context)
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

    def publish_envelope(self, routing_key, payload):
        bundle, username, password = self.current_publish_credentials()
        credentials = pika.PlainCredentials(username, password)
        parameters = pika.ConnectionParameters(
            host=bundle["hub"]["host"],
            port=int(bundle["hub"]["amqpPort"]),
            virtual_host=bundle["hub"]["vhost"],
            credentials=credentials,
            heartbeat=max(self.publish_interval * 2, 30),
            blocked_connection_timeout=30,
            client_properties={
                "connection_name": f"{self.pod_namespace}/{self.pod_name}/relay-publisher",
            },
        )
        connection = pika.BlockingConnection(parameters)
        try:
            channel = connection.channel()
            exchange = bundle["hub"]["exchanges"]["data"]
            channel.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
            channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
                properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
            )
        finally:
            connection.close()

    def normalize_command_headers(self, headers):
        result = {
            "Accept": headers.get("Accept", "application/json"),
            "Content-Type": headers.get("Content-Type", "application/json"),
        }
        for header_name in ["Content-Encoding", "X-Uncompressed-Length"]:
            value = headers.get(header_name, "").strip()
            if value:
                result[header_name] = value
        return result

    def handle_local_command_submission(self, parsed, headers, body):
        if not self.command_proxy_enabled:
            raise RelayLocalCommandError(404, "relay command proxy is disabled")

        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        command = (params.get("command") or [""])[0].strip()
        canonical_command = normalize_command_name(command)
        version = (params.get("version") or [""])[0].strip()
        certname = (params.get("certname") or [""])[0].strip()
        producer_timestamp = (params.get("producer-timestamp") or [""])[0].strip()
        if not command or not version or not certname or not producer_timestamp:
            raise RelayLocalCommandError(400, "missing required PuppetDB command parameters")

        query_string = parsed.query
        request_headers = self.normalize_command_headers(headers)
        payload_b64 = base64.b64encode(body).decode("ascii")
        message_id = sha256_text(
            "\n".join(
                [
                    parsed.path,
                    query_string,
                    json.dumps(request_headers, sort_keys=True),
                    payload_b64,
                ]
            )
        )
        response_uuid = str(uuid.uuid4())

        if canonical_command not in self.replicated_commands:
            self.set_status(
                localSkippedCommandCount=int(self.snapshot_status().get("localSkippedCommandCount") or 0) + 1
            )
            return {
                "command": command,
                "canonical_command": canonical_command,
                "message_id": message_id,
                "replicated": False,
                "uuid": response_uuid,
            }

        envelope = {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorRelayCommand",
            "messageId": message_id,
            "publishedAt": int(time.time()),
            "origin": {
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "role": self.relay_role,
                "segment": self.segment_name,
            },
            "targetRoles": list(self.command_target_roles),
            "command": {
                "name": command,
                "canonicalName": canonical_command,
                "version": version,
                "certname": certname,
                "producerTimestamp": producer_timestamp,
            },
            "request": {
                "path": parsed.path,
                "query": query_string,
                "headers": request_headers,
                "bodyBase64": payload_b64,
            },
        }

        try:
            self.publish_envelope(
                f"relay.command.{sanitize_fragment(canonical_command)}",
                envelope,
            )
        except Exception as error:
            raise RelayLocalCommandError(503, f"failed to publish relay command: {error}") from error

        status = self.snapshot_status()
        self.set_status(
            localPublishedCommandCount=int(status.get("localPublishedCommandCount") or 0) + 1,
            lastCommandPublishedAt=int(time.time()),
            lastReplayError="",
        )
        log(f"Published relay command '{command}' for {certname}")
        return {
            "command": command,
            "canonical_command": canonical_command,
            "message_id": message_id,
            "replicated": True,
            "uuid": response_uuid,
        }

    def has_processed_message(self, message_id):
        return os.path.isfile(self.processed_message_path(message_id))

    def mark_processed_message(self, message_id, payload):
        write_text_file(
            self.processed_message_path(message_id),
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def replay_remote_command(self, payload):
        request = payload.get("request") or {}
        query_string = (request.get("query") or "").strip()
        url = self.local_puppetdb_command_url
        if query_string:
            url = f"{url}?{query_string}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        headers.update(request.get("headers") or {})
        body = base64.b64decode((request.get("bodyBase64") or "").encode("ascii"))
        context = self.build_local_puppetdb_context()
        status_code, response_body = http_request_raw(
            "POST",
            url,
            headers=headers,
            data=body,
            context=context,
        )
        if status_code not in {200, 202}:
            raise RuntimeError(
                f"local PuppetDB replay returned {status_code}: {response_body}"
            )
        return response_body

    def handle_remote_command(self, payload):
        origin = payload.get("origin") or {}
        if (origin.get("participant") or "").strip() == self.pod_name:
            return

        target_roles = payload.get("targetRoles") or []
        if target_roles and self.relay_role not in target_roles:
            return

        command = ((payload.get("command") or {}).get("name") or "").strip()
        canonical_command = normalize_command_name(
            ((payload.get("command") or {}).get("canonicalName") or command).strip()
        )
        if canonical_command not in self.replicated_commands:
            return

        message_id = (payload.get("messageId") or "").strip()
        if not message_id:
            raise RuntimeError("relay command message is missing messageId")

        if self.has_processed_message(message_id):
            return

        self.replay_remote_command(payload)
        self.mark_processed_message(message_id, payload)
        status = self.snapshot_status()
        certname = ((payload.get("command") or {}).get("certname") or "").strip()
        self.set_status(
            replayedCommandCount=int(status.get("replayedCommandCount") or 0) + 1,
            lastCommandReplayedAt=int(time.time()),
            lastReplayError="",
        )
        log(
            f"Replayed relay command '{canonical_command}' for {certname} "
            f"from {origin.get('participant') or 'unknown'}"
        )

    def on_message(self, channel, method, _properties, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            channel.basic_ack(method.delivery_tag)
            return

        kind = payload.get("kind")
        if kind == "ConductorRelayStatus":
            status = payload.get("status") or {}
            participant_name = (status.get("participant") or "").strip()
            if participant_name and participant_name != self.pod_name:
                write_text_file(
                    self.peer_status_path(participant_name),
                    json.dumps(status, indent=2, sort_keys=True) + "\n",
                )
                self.set_status(lastReceivedAt=int(time.time()))
            channel.basic_ack(method.delivery_tag)
            return

        if kind == "ConductorRelayCommand":
            try:
                self.handle_remote_command(payload)
                self.set_status(lastReceivedAt=int(time.time()))
                channel.basic_ack(method.delivery_tag)
            except Exception as error:
                status = self.snapshot_status()
                self.set_status(
                    replayFailureCount=int(status.get("replayFailureCount") or 0) + 1,
                    lastReplayError=str(error),
                )
                log(f"Remote relay command replay failed: {error}")
                time.sleep(5)
                channel.basic_nack(method.delivery_tag, requeue=True)
            return

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
        self.set_status(lastPublishedAt=now)
        payload_status = self.snapshot_status()
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
                self.ensure_command_proxy_running()
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
