#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import re
import ssl
import stat
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
from cryptography import x509


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
DEFAULT_CA_SYNC_DIR = "/etc/puppetlabs/puppetserver/ca"


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


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


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


def write_bytes_file(path, content):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "wb") as handle:
        handle.write(content)
    os.replace(temp_path, path)


def normalize_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def datetime_sort_value(value):
    value = normalize_datetime(value)
    return int(value.timestamp()) if value is not None else 0


def split_pem_blocks(content, label):
    pattern = re.compile(
        rf"-----BEGIN {re.escape(label)}-----\s*.*?-----END {re.escape(label)}-----\s*",
        re.DOTALL,
    )
    blocks = []
    for match in pattern.finditer(content or ""):
        block = match.group(0).strip()
        if block:
            blocks.append(block + "\n")
    return blocks


def merge_crl_pem_bundles(contents):
    selected = {}
    for content in contents:
        for block in split_pem_blocks(content, "X509 CRL"):
            try:
                crl = x509.load_pem_x509_crl(block.encode("utf-8"))
            except ValueError as error:
                raise RuntimeError(f"invalid PEM CRL bundle entry: {error}") from error
            issuer = crl.issuer.rfc4514_string()
            try:
                crl_number = crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
            except x509.ExtensionNotFound:
                crl_number = 0
            sort_key = (
                crl_number,
                datetime_sort_value(getattr(crl, "next_update", None)),
                datetime_sort_value(getattr(crl, "last_update", None)),
                sha256_text(block),
            )
            current = selected.get(issuer)
            if current is None or sort_key > current[0]:
                selected[issuer] = (sort_key, block)
    return "".join(selected[issuer][1] for issuer in sorted(selected))


def certificate_sort_key(pem_bytes):
    cert = x509.load_pem_x509_certificate(pem_bytes)
    return (
        datetime_sort_value(getattr(cert, "not_valid_before", None)),
        datetime_sort_value(getattr(cert, "not_valid_after", None)),
        cert.serial_number,
        sha256_bytes(pem_bytes),
    )


def normalize_pem_entry_name(value):
    name = (value or "").strip()
    if not name or name != os.path.basename(name) or "/" in name or "\\" in name:
        return ""
    if not name.endswith(".pem"):
        return ""
    return name


def parse_hex_serial(serial_bytes):
    text = serial_bytes.decode("utf-8")
    stripped = text.strip()
    if not stripped:
        return 0, max(len(text), 2), text.endswith("\n")
    return int(stripped, 16), max(len(stripped), 2), text.endswith("\n")


def render_hex_serial(value, width, trailing_newline):
    rendered = f"{value:0{max(width, 2)}X}"
    if trailing_newline:
        rendered += "\n"
    return rendered.encode("utf-8")

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
    if status.get("caSyncEnabled", False) and not status.get("caSyncReady", False):
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
        self.ca_peer_dir = os.path.join(self.output_dir, "ca-peers")
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
        self.ca_sync_enabled = env_bool("CONDUCTOR_RELAY_CA_SYNC_ENABLED", False)
        self.ca_sync_dir = (
            os.environ.get("CONDUCTOR_RELAY_CA_SYNC_DIR", "").strip()
            or DEFAULT_CA_SYNC_DIR
        )
        self.ca_sync_publish_interval = env_int(
            "CONDUCTOR_RELAY_CA_SYNC_PUBLISH_INTERVAL_SECONDS",
            self.publish_interval,
        )
        self.ca_sync_max_age = env_int(
            "CONDUCTOR_RELAY_CA_SYNC_MAX_AGE_SECONDS",
            max(self.publish_interval * 4, 60),
        )
        self.ca_sync_target_roles = env_csv(
            "CONDUCTOR_RELAY_CA_SYNC_TARGET_ROLES",
            default=DEFAULT_TARGET_ROLES,
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
        self.next_ca_publish = 0
        self.last_published_ca_hash = ""

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
            "caSyncEnabled": self.ca_sync_enabled,
            "caSyncReady": not self.ca_sync_enabled,
            "caSyncDir": self.ca_sync_dir if self.ca_sync_enabled else "",
            "caSyncTargetRoles": list(self.ca_sync_target_roles),
            "caSyncPeerStateCount": 0,
            "caSyncRequestCount": 0,
            "caSyncSignedCount": 0,
            "caSyncPublishedStateCount": 0,
            "caSyncAppliedStateCount": 0,
            "caSyncLastPublishedAt": 0,
            "caSyncLastAppliedAt": 0,
            "caSyncStateHash": "",
            "caSyncLastError": "",
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

    def ca_peer_state_path(self, participant_name):
        filename = f"{sanitize_fragment(participant_name)}.json"
        return os.path.join(self.ca_peer_dir, filename)

    def ca_requests_dir(self):
        return os.path.join(self.ca_sync_dir, "requests")

    def ca_signed_dir(self):
        return os.path.join(self.ca_sync_dir, "signed")

    def ca_inventory_path(self):
        return os.path.join(self.ca_sync_dir, "inventory.txt")

    def ca_serial_path(self):
        return os.path.join(self.ca_sync_dir, "serial")

    def ca_crl_path(self):
        return os.path.join(self.ca_sync_dir, "ca_crl.pem")

    def ca_infra_crl_path(self):
        return os.path.join(self.ca_sync_dir, "infra_crl.pem")

    def ca_owner(self):
        owner = os.stat(self.ca_sync_dir)
        return owner.st_uid, owner.st_gid

    def ensure_owned_directory(self, path, mode):
        uid, gid = self.ca_owner()
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
        os.chown(path, uid, gid)
        os.chmod(path, mode)

    def write_owned_bytes_file(self, path, content, default_mode):
        uid, gid = self.ca_owner()
        existing_mode = default_mode
        if os.path.exists(path):
            existing_mode = stat.S_IMODE(os.stat(path).st_mode)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_path = f"{path}.tmp-{uuid.uuid4().hex}"
        try:
            with open(temp_path, "wb") as handle:
                handle.write(content)
            os.chown(temp_path, uid, gid)
            os.chmod(temp_path, existing_mode)
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    @staticmethod
    def decode_file_map(payload):
        result = {}
        for raw_name, encoded in (payload or {}).items():
            name = normalize_pem_entry_name(raw_name)
            if not name or not isinstance(encoded, str):
                continue
            result[name] = base64.b64decode(encoded.encode("ascii"))
        return result

    @staticmethod
    def merge_inventory_text(local_text, remote_text):
        lines = []
        seen = set()
        for source in [local_text, remote_text]:
            for line in source.splitlines():
                cleaned = line.strip()
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                lines.append(cleaned)
        return "".join(f"{line}\n" for line in lines)

    @staticmethod
    def preferred_signed_bytes(local_bytes, remote_bytes):
        if local_bytes == remote_bytes:
            return local_bytes
        try:
            return remote_bytes if certificate_sort_key(remote_bytes) >= certificate_sort_key(local_bytes) else local_bytes
        except ValueError:
            return remote_bytes

    def read_ca_state(self):
        if not self.ca_sync_enabled:
            return None

        self.ensure_owned_directory(self.ca_requests_dir(), 0o700)
        self.ensure_owned_directory(self.ca_signed_dir(), 0o750)

        requests = {}
        for entry in sorted(os.scandir(self.ca_requests_dir()), key=lambda item: item.name):
            if not entry.is_file():
                continue
            name = normalize_pem_entry_name(entry.name)
            if not name:
                continue
            with open(entry.path, "rb") as handle:
                requests[name] = base64.b64encode(handle.read()).decode("ascii")

        signed = {}
        for entry in sorted(os.scandir(self.ca_signed_dir()), key=lambda item: item.name):
            if not entry.is_file():
                continue
            name = normalize_pem_entry_name(entry.name)
            if not name:
                continue
            with open(entry.path, "rb") as handle:
                signed[name] = base64.b64encode(handle.read()).decode("ascii")

        serial_bytes = b""
        if os.path.isfile(self.ca_serial_path()):
            with open(self.ca_serial_path(), "rb") as handle:
                serial_bytes = handle.read()
        inventory_bytes = b""
        if os.path.isfile(self.ca_inventory_path()):
            with open(self.ca_inventory_path(), "rb") as handle:
                inventory_bytes = handle.read()
        ca_crl_bytes = b""
        if os.path.isfile(self.ca_crl_path()):
            with open(self.ca_crl_path(), "rb") as handle:
                ca_crl_bytes = handle.read()
        infra_crl_bytes = b""
        if os.path.isfile(self.ca_infra_crl_path()):
            with open(self.ca_infra_crl_path(), "rb") as handle:
                infra_crl_bytes = handle.read()

        state = {
            "requests": requests,
            "signed": signed,
            "inventoryTxtBase64": base64.b64encode(inventory_bytes).decode("ascii"),
            "serialBase64": base64.b64encode(serial_bytes).decode("ascii"),
            "caCrlPemBase64": base64.b64encode(ca_crl_bytes).decode("ascii"),
            "infraCrlPemBase64": base64.b64encode(infra_crl_bytes).decode("ascii"),
        }
        state["hash"] = sha256_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
        return state

    def write_ca_peer_summary(self, participant, published_at, state):
        summary = {
            "participant": participant,
            "publishedAt": published_at,
            "hash": (state or {}).get("hash", ""),
            "requestCount": len((state or {}).get("requests") or {}),
            "signedCount": len((state or {}).get("signed") or {}),
            "lastAppliedAt": int(time.time()),
        }
        write_text_file(
            self.ca_peer_state_path(participant),
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
        )

    def apply_remote_ca_state(self, payload):
        if not self.ca_sync_enabled:
            return

        origin = payload.get("origin") or {}
        participant = (origin.get("participant") or "").strip()
        if not participant or participant == self.pod_name:
            return

        target_roles = payload.get("targetRoles") or []
        if target_roles and self.relay_role not in target_roles:
            return

        published_at = int(payload.get("publishedAt") or 0)
        existing_summary_path = self.ca_peer_state_path(participant)
        if os.path.isfile(existing_summary_path):
            try:
                existing_summary = read_json_file(existing_summary_path)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                existing_summary = {}
            if published_at and published_at <= int(existing_summary.get("publishedAt") or 0):
                return

        state = payload.get("state") or {}
        remote_requests = self.decode_file_map(state.get("requests"))
        remote_signed = self.decode_file_map(state.get("signed"))
        remote_inventory_bytes = base64.b64decode((state.get("inventoryTxtBase64") or "").encode("ascii"))
        remote_serial_bytes = base64.b64decode((state.get("serialBase64") or "").encode("ascii"))
        remote_ca_crl_bytes = base64.b64decode((state.get("caCrlPemBase64") or "").encode("ascii"))
        remote_infra_crl_bytes = base64.b64decode((state.get("infraCrlPemBase64") or "").encode("ascii"))

        updates = {
            "requests": 0,
            "signed": 0,
            "inventory": 0,
            "serial": 0,
            "ca_crl": 0,
            "infra_crl": 0,
        }

        for name, remote_bytes in remote_signed.items():
            path = os.path.join(self.ca_signed_dir(), name)
            local_bytes = b""
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    local_bytes = handle.read()
            selected_bytes = self.preferred_signed_bytes(local_bytes, remote_bytes) if local_bytes else remote_bytes
            if local_bytes != selected_bytes:
                self.write_owned_bytes_file(path, selected_bytes, 0o644)
                updates["signed"] += 1
            request_path = os.path.join(self.ca_requests_dir(), name)
            if os.path.isfile(request_path):
                os.unlink(request_path)
                updates["requests"] += 1

        for name, remote_bytes in remote_requests.items():
            if os.path.isfile(os.path.join(self.ca_signed_dir(), name)):
                continue
            path = os.path.join(self.ca_requests_dir(), name)
            local_bytes = b""
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    local_bytes = handle.read()
            if local_bytes != remote_bytes:
                self.write_owned_bytes_file(path, remote_bytes, 0o640)
                updates["requests"] += 1

        local_inventory_text = ""
        if os.path.isfile(self.ca_inventory_path()):
            with open(self.ca_inventory_path(), "r", encoding="utf-8") as handle:
                local_inventory_text = handle.read()
        remote_inventory_text = remote_inventory_bytes.decode("utf-8")
        merged_inventory_text = self.merge_inventory_text(local_inventory_text, remote_inventory_text)
        if merged_inventory_text != local_inventory_text:
            self.write_owned_bytes_file(
                self.ca_inventory_path(),
                merged_inventory_text.encode("utf-8"),
                0o640,
            )
            updates["inventory"] += 1

        local_serial_bytes = b""
        if os.path.isfile(self.ca_serial_path()):
            with open(self.ca_serial_path(), "rb") as handle:
                local_serial_bytes = handle.read()
        if remote_serial_bytes:
            local_serial_value, local_serial_width, local_has_newline = parse_hex_serial(local_serial_bytes)
            remote_serial_value, remote_serial_width, remote_has_newline = parse_hex_serial(remote_serial_bytes)
            if remote_serial_value > local_serial_value:
                self.write_owned_bytes_file(
                    self.ca_serial_path(),
                    render_hex_serial(
                        remote_serial_value,
                        max(local_serial_width, remote_serial_width),
                        local_has_newline or remote_has_newline,
                    ),
                    0o644,
                )
                updates["serial"] += 1

        if remote_ca_crl_bytes:
            local_ca_crl_text = ""
            if os.path.isfile(self.ca_crl_path()):
                with open(self.ca_crl_path(), "r", encoding="utf-8") as handle:
                    local_ca_crl_text = handle.read()
            merged_ca_crl_text = merge_crl_pem_bundles(
                [local_ca_crl_text, remote_ca_crl_bytes.decode("utf-8")]
            )
            if merged_ca_crl_text != local_ca_crl_text:
                self.write_owned_bytes_file(
                    self.ca_crl_path(),
                    merged_ca_crl_text.encode("utf-8"),
                    0o640,
                )
                updates["ca_crl"] += 1

        if remote_infra_crl_bytes:
            local_infra_crl_text = ""
            if os.path.isfile(self.ca_infra_crl_path()):
                with open(self.ca_infra_crl_path(), "r", encoding="utf-8") as handle:
                    local_infra_crl_text = handle.read()
            merged_infra_crl_text = merge_crl_pem_bundles(
                [local_infra_crl_text, remote_infra_crl_bytes.decode("utf-8")]
            )
            if merged_infra_crl_text != local_infra_crl_text:
                self.write_owned_bytes_file(
                    self.ca_infra_crl_path(),
                    merged_infra_crl_text.encode("utf-8"),
                    0o640,
                )
                updates["infra_crl"] += 1

        self.write_ca_peer_summary(participant, published_at, state)
        status = self.snapshot_status()
        self.set_status(
            caSyncReady=True,
            caSyncAppliedStateCount=int(status.get("caSyncAppliedStateCount") or 0) + 1,
            caSyncLastAppliedAt=int(time.time()),
            caSyncLastError="",
        )
        updated_items = [name for name, count in updates.items() if count > 0]
        if updated_items:
            log(
                f"Applied CA state from {participant}: "
                + ", ".join(f"{name}={updates[name]}" for name in updated_items)
            )

    def publish_ca_state(self):
        if not self.ca_sync_enabled or self.connection is None or self.channel is None or self.bundle is None:
            return

        state = self.read_ca_state()
        now = int(time.time())
        state_hash = state.get("hash", "")
        self.set_status(
            caSyncReady=True,
            caSyncStateHash=state_hash,
            caSyncRequestCount=len(state.get("requests") or {}),
            caSyncSignedCount=len(state.get("signed") or {}),
            caSyncLastError="",
        )

        should_publish = state_hash != self.last_published_ca_hash or now >= self.next_ca_publish
        if not should_publish:
            return

        payload = {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorRelayCaState",
            "publishedAt": now,
            "origin": {
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "role": self.relay_role,
                "segment": self.segment_name,
            },
            "targetRoles": list(self.ca_sync_target_roles),
            "state": state,
        }
        routing_key = f"relay.ca-state.{sanitize_fragment(self.pod_name)}"
        self.channel.basic_publish(
            exchange=self.bundle["hub"]["exchanges"]["data"],
            routing_key=routing_key,
            body=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )
        status = self.snapshot_status()
        self.set_status(
            caSyncPublishedStateCount=int(status.get("caSyncPublishedStateCount") or 0) + 1,
            caSyncLastPublishedAt=now,
        )
        self.last_published_ca_hash = state_hash
        self.next_ca_publish = now + self.ca_sync_publish_interval

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

    def refresh_ca_peer_counts(self):
        if not self.ca_sync_enabled:
            self.set_status(caSyncPeerStateCount=0)
            return

        fresh_statuses = []
        os.makedirs(self.ca_peer_dir, exist_ok=True)
        now = int(time.time())
        for entry in os.scandir(self.ca_peer_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                status = read_json_file(entry.path)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            published_at = int(status.get("publishedAt") or 0)
            if published_at <= 0:
                continue
            if now - published_at > self.ca_sync_max_age:
                continue
            fresh_statuses.append(status)

        self.set_status(caSyncPeerStateCount=len(fresh_statuses))

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

        if kind == "ConductorRelayCaState":
            try:
                self.apply_remote_ca_state(payload)
                self.set_status(lastReceivedAt=int(time.time()))
                channel.basic_ack(method.delivery_tag)
            except Exception as error:
                self.set_status(
                    caSyncReady=False,
                    caSyncLastError=str(error),
                )
                log(f"Remote CA state apply failed: {error}")
                time.sleep(2)
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
        self.next_ca_publish = 0
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
        self.refresh_ca_peer_counts()
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
                    self.publish_ca_state()
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
            self.refresh_ca_peer_counts()
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
