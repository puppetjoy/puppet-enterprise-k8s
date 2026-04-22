#!/usr/bin/env python3

import base64
import hashlib
import http.client
import json
import os
import queue
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

import pg8000.dbapi
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pika
from cryptography import x509
try:
    from cassandra import ConsistencyLevel
    from cassandra.cluster import Cluster
    from cassandra.query import SimpleStatement
except ImportError:  # pragma: no cover - optional dependency until Cassandra-backed slices are enabled
    Cluster = None
    ConsistencyLevel = None
    SimpleStatement = None


DEFAULT_STATUS_FILENAME = "relay-status.json"
DEFAULT_REPLICATED_COMMANDS = [
    "replace facts",
    "store report",
    "deactivate node",
]
DEFAULT_TARGET_ROLES = [
    "control-plane",
]
DEFAULT_CODE_DEPLOY_TARGET_ROLES = [
    "control-plane",
]
DEFAULT_CLASSIFIER_TARGET_ROLES = [
    "control-plane",
]
DEFAULT_COMMAND_PROXY_PATH = "/pdb/cmd/v1"
DEFAULT_CA_SYNC_DIR = "/etc/puppetlabs/puppetserver/ca"
DEFAULT_CODE_DEPLOY_HOOK_PATH = "/conductor/code-manager/v1/post-environment"
DEFAULT_CODE_DEPLOY_STATE_FILENAME = "code-deploy-state.json"
DEFAULT_CLASSIFIER_SYNC_STATE_FILENAME = "classifier-sync-state.json"
DEFAULT_CLASSIFIER_SYNC_SCOPE = "filtered-all-nodes"
DEFAULT_CLASSIFIER_SYNC_CASSANDRA_KEYSPACE = "conductor_classifier"
DEFAULT_CLASSIFIER_SYNC_CASSANDRA_TABLE = "classifier_state"
FRONTDOOR_ELIGIBLE_ANNOTATION = "pe-k8s.puppet.com/frontdoor-eligible"
FRONTDOOR_BLOCKERS_ANNOTATION = "pe-k8s.puppet.com/frontdoor-blockers"
FRONTDOOR_REASON_ANNOTATION = "pe-k8s.puppet.com/frontdoor-reason"
FRONTDOOR_UPDATED_AT_ANNOTATION = "pe-k8s.puppet.com/frontdoor-updated-at"
ALL_NODES_GROUP_ID = "00000000-0000-4000-8000-000000000000"
LEGACY_CLASSIFIER_ROOT_GROUP_ID = "f6b0f884-0fb8-4f5b-9cf8-0d430711f4d2"
LEGACY_CLASSIFIER_ROOT_GROUP_NAME = "Conductor Shared Classification"
CLASSIFIER_LOGICAL_ALL_ENVIRONMENTS_ID = "builtin://all-environments"
CLASSIFIER_LOGICAL_PRODUCTION_ENVIRONMENT_ID = "builtin://production-environment"
CLASSIFIER_LOGICAL_DEVELOPMENT_ENVIRONMENT_ID = "builtin://development-environment"
CLASSIFIER_LOGICAL_DEVELOPMENT_ONE_TIME_RUN_EXCEPTION_ID = (
    "builtin://development-one-time-run-exception"
)
CLASSIFIER_LOGICAL_PE_PATCH_MANAGEMENT_ID = "builtin://pe-patch-management"
CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES = {
    "PE Infrastructure",
    LEGACY_CLASSIFIER_ROOT_GROUP_NAME,
}
DEFAULT_RBAC_TARGET_ROLES = [
    "control-plane",
]
DEFAULT_RBAC_SYNC_STATE_FILENAME = "rbac-sync-state.json"
DEFAULT_RBAC_SYNC_SCOPE = "pe-rbac-managed-domain"
DEFAULT_RBAC_TOKEN_SYNC_STATE_FILENAME = "rbac-token-sync-state.json"
DEFAULT_RBAC_TOKEN_SYNC_SCOPE = "pe-rbac-token-auth"
DEFAULT_RBAC_SYNC_RBAC_CONF_PATH = "/etc/puppetlabs/console-services/conf.d/rbac.conf"
DEFAULT_RBAC_SYNC_RBAC_DATABASE_CONF_PATH = "/etc/puppetlabs/console-services/conf.d/rbac-database.conf"
DEFAULT_RBAC_SYNC_KEYS_PATH = "/etc/puppetlabs/console-services/conf.d/secrets/keys.json"
DEFAULT_RBAC_SYNC_SHARED_SECRET_DIR = "/etc/puppetlabs/console-services/conf.d/secrets/conductor"
DEFAULT_RBAC_SYNC_SHARED_TOKEN_PRIVATE_KEY_PATH = (
    f"{DEFAULT_RBAC_SYNC_SHARED_SECRET_DIR}/token-signing.private_key.pem"
)
DEFAULT_RBAC_SYNC_SHARED_TOKEN_PUBLIC_KEY_PATH = (
    f"{DEFAULT_RBAC_SYNC_SHARED_SECRET_DIR}/token-signing.cert.pem"
)
DEFAULT_RBAC_SYNC_SHARED_SAML_KEY_PATH = (
    f"{DEFAULT_RBAC_SYNC_SHARED_SECRET_DIR}/saml.private_key.pem"
)
DEFAULT_RBAC_SYNC_SHARED_SAML_CERT_PATH = (
    f"{DEFAULT_RBAC_SYNC_SHARED_SECRET_DIR}/saml.cert.pem"
)
DEFAULT_RBAC_SYNC_EXCLUDED_TOKEN_LABEL_PREFIXES = [
    "pe-k8s-conductor-",
    "pe-k8s-classifier",
    "pe-k8s-compiler certificate",
]
DEFAULT_RBAC_SYNC_CASSANDRA_KEYSPACE = "conductor_auth"
DEFAULT_RBAC_SYNC_CASSANDRA_TABLE = "rbac_state"
DEFAULT_RBAC_TOKEN_SYNC_CASSANDRA_KEYSPACE = "conductor_auth"
DEFAULT_RBAC_TOKEN_SYNC_CASSANDRA_TABLE = "rbac_token_state"
RBAC_SYNC_TABLE_NAMES = [
    "configuration",
    "external_access_config",
    "permissions",
    "roles",
    "roles_permissions",
    "salt",
    "subjects",
    "subject_roles",
    "groupings",
    "password_history",
    "password_reset_tokens",
    "tokens",
]
RBAC_SYNC_SELECT_QUERIES = {
    "configuration": """
        select id, kind, data, created_at, modified_at
        from configuration
        order by kind, id
    """,
    "external_access_config": """
        select
            id,
            config_type,
            display_name,
            creation_date,
            last_updated,
            data_element,
            coalesce(encode(secrets, 'base64'), '') as secrets_base64,
            encryption_key_id
        from external_access_config
        order by config_type, display_name, id
    """,
    "permissions": """
        select id, object_type, action, instance
        from permissions
        order by object_type, action, instance, id
    """,
    "roles": """
        select id, display_name, description
        from roles
        order by id
    """,
    "roles_permissions": """
        select rid, pid
        from roles_permissions
        order by rid, pid
    """,
    "salt": """
        select id, salt
        from salt
        order by id
    """,
    "subjects": """
        select
            id,
            login,
            is_group,
            is_remote,
            is_superuser,
            display_name,
            email,
            is_revoked,
            last_login,
            password,
            reset_password_uuid,
            failed_login_attempts,
            created_at,
            external_access_id,
            is_immutable
        from subjects
        order by login, id
    """,
    "subject_roles": """
        select sid, rid
        from subject_roles
        order by sid, rid
    """,
    "groupings": """
        select gid, ruid
        from groupings
        order by gid, ruid
    """,
    "password_history": """
        select id, sid, replacement_date, password
        from password_history
        order by sid, replacement_date, id
    """,
    "password_reset_tokens": """
        select token, sid, requestor, expiration_date, creation_date
        from password_reset_tokens
        order by sid, token
    """,
    "tokens": """
        select
            id,
            expiration,
            user_id,
            token,
            label,
            creation,
            client,
            description,
            timeout,
            coalesce(last_active, creation) as last_active,
            token_hash
        from tokens
        order by id
    """,
}
RBAC_SYNC_REQUIRED_AUTH_FILES = {
    "keysJson": DEFAULT_RBAC_SYNC_KEYS_PATH,
    "tokenPrivateKey": DEFAULT_RBAC_SYNC_SHARED_TOKEN_PRIVATE_KEY_PATH,
    "tokenPublicKey": DEFAULT_RBAC_SYNC_SHARED_TOKEN_PUBLIC_KEY_PATH,
}
RBAC_SYNC_OPTIONAL_AUTH_FILES = {
    "samlKey": DEFAULT_RBAC_SYNC_SHARED_SAML_KEY_PATH,
    "samlCert": DEFAULT_RBAC_SYNC_SHARED_SAML_CERT_PATH,
}
RBAC_SYNC_VOLATILE_FIELDS = {
    "subjects": {"last_login"},
    "tokens": {"last_active"},
}
RBAC_TOKEN_SYNC_TABLE_NAMES = [
    "tokens",
]
DEFAULT_ORCHESTRATION_TARGET_ROLES = [
    "control-plane",
]
DEFAULT_ORCHESTRATION_SYNC_STATE_FILENAME = "orchestration-sync-state.json"
DEFAULT_ORCHESTRATION_SYNC_SCOPE = "pe-orchestration-managed-domain"
DEFAULT_ORCHESTRATION_SYNC_ORCHESTRATOR_CONF_PATH = (
    "/etc/puppetlabs/orchestration-services/conf.d/orchestrator.conf"
)
DEFAULT_ORCHESTRATION_SYNC_INVENTORY_CONF_PATH = (
    "/etc/puppetlabs/orchestration-services/conf.d/inventory.conf"
)
DEFAULT_ORCHESTRATION_SYNC_KEYS_PATH = (
    "/etc/puppetlabs/orchestration-services/conf.d/secrets/keys.json"
)
DEFAULT_ORCHESTRATION_SYNC_ENCRYPTION_STORE_PATH = (
    "/etc/puppetlabs/orchestration-services/conf.d/secrets/orchestrator-encryption-keys.json"
)
DEFAULT_ORCHESTRATION_SYNC_SEQUENCE_STRIDE = 1024
DEFAULT_ORCHESTRATION_SYNC_CASSANDRA_KEYSPACE = "conductor_orchestration"
DEFAULT_ORCHESTRATION_SYNC_CASSANDRA_TABLE = "orchestrator_state"
DEFAULT_INVENTORY_SYNC_STATE_FILENAME = "inventory-sync-state.json"
DEFAULT_INVENTORY_SYNC_SCOPE = "pe-orchestration-inventory"
DEFAULT_INVENTORY_SYNC_CASSANDRA_KEYSPACE = "conductor_orchestration"
DEFAULT_INVENTORY_SYNC_CASSANDRA_TABLE = "inventory_state"
ORCHESTRATION_SYNC_REQUIRED_AUTH_FILES = {
    "inventoryKeysJson": DEFAULT_ORCHESTRATION_SYNC_KEYS_PATH,
    "orchestratorEncryptionStore": DEFAULT_ORCHESTRATION_SYNC_ENCRYPTION_STORE_PATH,
}
INVENTORY_SYNC_REQUIRED_AUTH_FILES = {
    "inventoryKeysJson": DEFAULT_ORCHESTRATION_SYNC_KEYS_PATH,
}
ORCHESTRATION_SYNC_DATABASE_SPECS = {
    "orchestrator": {
        "tables": {
            "encrypted_data": {
                "columns": [
                    ("id", "integer"),
                    ("encryption_key_id", "text"),
                    ("encrypted_data", "text"),
                ],
                "orderBy": ["id"],
            },
            "plan_jobs": {
                "columns": [
                    ("id", "integer"),
                    ("plan_name", "text"),
                    ("description", "text"),
                    ("result", "jsonb"),
                    ("status", "text"),
                    ("parameters", "jsonb"),
                    ("owner", "jsonb"),
                    ("created_timestamp", "timestamp with time zone"),
                    ("finished_timestamp", "timestamp with time zone"),
                    ("sensitive", "text[]"),
                    ("environment", "text"),
                    ("project_id", "text"),
                    ("ref", "text"),
                    ("userdata", "jsonb"),
                    ("timeout", "integer"),
                    ("encrypted_input_id", "integer"),
                ],
                "orderBy": ["id"],
            },
            "jobs": {
                "columns": [
                    ("id", "integer"),
                    ("environment", "text"),
                    ("owner", "jsonb"),
                    ("options", "jsonb"),
                    ("command", "text"),
                    ("files", "jsonb"),
                    ("metadata", "jsonb"),
                    ("description", "text"),
                    ("plan_job_id", "integer"),
                    ("project_id", "text"),
                    ("ref", "text"),
                    ("userdata", "jsonb"),
                ],
                "orderBy": ["id"],
            },
            "job_statuses": {
                "columns": [
                    ("id", "integer"),
                    ("state", "text"),
                    ("changed_by", "text"),
                    ("enter_time", "timestamp without time zone"),
                    ("exit_time", "timestamp without time zone"),
                    ("job_id", "integer"),
                ],
                "orderBy": ["id"],
            },
            "nodes": {
                "columns": [
                    ("id", "integer"),
                    ("name", "text"),
                    ("transaction_uuid", "uuid"),
                    ("state", "text"),
                    ("job_id", "integer"),
                    ("start_timestamp", "timestamp with time zone"),
                    ("finish_timestamp", "timestamp with time zone"),
                    ("transport", "text"),
                ],
                "orderBy": ["id"],
            },
            "events": {
                "columns": [
                    ("id", "integer"),
                    ("type", "text"),
                    ("details", "jsonb"),
                    ("timestamp", "timestamp with time zone"),
                    ("job_id", "integer"),
                    ("encrypted_output_id", "integer"),
                ],
                "orderBy": ["id"],
            },
            "permitted_nodes_for_plan": {
                "columns": [
                    ("id", "integer"),
                    ("permitted_nodes", "text[]"),
                    ("all_nodes", "boolean"),
                    ("plan_id", "integer"),
                ],
                "orderBy": ["id"],
            },
            "plan_events": {
                "columns": [
                    ("id", "integer"),
                    ("type", "text"),
                    ("details", "jsonb"),
                    ("timestamp", "timestamp with time zone"),
                    ("plan_id", "integer"),
                ],
                "orderBy": ["id"],
            },
            "sources": {
                "columns": [
                    ("id", "uuid"),
                    ("name", "text"),
                    ("project_ref", "text"),
                    ("last_synced", "timestamp with time zone"),
                    ("encryption_key_id", "text"),
                    ("source_type", "text"),
                    ("source_data", "text"),
                    ("target_count", "integer"),
                    ("description", "text"),
                ],
                "orderBy": ["name", "id"],
            },
            "targets": {
                "columns": [
                    ("name", "text"),
                    ("uri", "text"),
                    ("config", "text"),
                    ("encryption_key_id", "text"),
                    ("source_id", "uuid"),
                    ("active", "boolean"),
                    ("created_at", "timestamp with time zone"),
                    ("last_synced", "timestamp with time zone"),
                ],
                "orderBy": ["source_id", "name", "uri"],
            },
        },
        "clearOrder": [
            "nodes",
            "job_statuses",
            "events",
            "jobs",
            "permitted_nodes_for_plan",
            "plan_events",
            "targets",
            "plan_jobs",
            "sources",
            "encrypted_data",
        ],
        "loadOrder": [
            "encrypted_data",
            "plan_jobs",
            "jobs",
            "job_statuses",
            "nodes",
            "events",
            "permitted_nodes_for_plan",
            "plan_events",
            "sources",
            "targets",
        ],
        "sequenceColumns": [
            ("encrypted_data", "id"),
            ("plan_jobs", "id"),
            ("jobs", "id"),
            ("job_statuses", "id"),
            ("nodes", "id"),
            ("events", "id"),
            ("permitted_nodes_for_plan", "id"),
            ("plan_events", "id"),
        ],
    },
    "inventory": {
        "tables": {
            "parameters": {
                "columns": [
                    ("id", "uuid"),
                    ("parameters", "jsonb"),
                ],
                "orderBy": ["id"],
            },
            "sensitive_parameters": {
                "columns": [
                    ("id", "uuid"),
                    ("encryption_key_id", "text"),
                    ("parameters", "text"),
                ],
                "orderBy": ["id"],
            },
            "connections": {
                "columns": [
                    ("id", "uuid"),
                    ("type", "text"),
                    ("create_time", "timestamp with time zone"),
                    ("parameters", "uuid"),
                    ("sensitive_parameters", "uuid"),
                    ("certnames", "text[]"),
                    ("undiscoverable", "boolean"),
                ],
                "orderBy": ["id"],
            },
            "connection_metadata": {
                "columns": [
                    ("id", "uuid"),
                    ("connection_id", "uuid"),
                    ("metadata", "jsonb"),
                    ("created_at", "timestamp with time zone"),
                    ("updated_at", "timestamp with time zone"),
                ],
                "orderBy": ["id"],
            },
        },
        "clearOrder": [
            "connection_metadata",
            "connections",
            "sensitive_parameters",
            "parameters",
        ],
        "loadOrder": [
            "parameters",
            "sensitive_parameters",
            "connections",
            "connection_metadata",
        ],
        "sequenceColumns": [],
    },
}
DEFAULT_CONSOLE_WEBSERVER_CONF_PATH = "/etc/puppetlabs/console-services/conf.d/webserver.conf"
DEFAULT_AUTH_BARRIER_SESSION_COOKIE_NAME = "__HOST-pl_sssi"
DEFAULT_AUTH_BARRIER_AUTH_COOKIE_NAME = "__HOST-pl_ssti"
DEFAULT_AUTH_BARRIER_LOGIN_PATH = "/auth/login"
DEFAULT_AUTH_BARRIER_TOKEN_PATH = "/rbac-api/v1/auth/token"
DEFAULT_AUTH_BARRIER_LOGINSESSION_PATH_PREFIX = "/conductor/auth/v1/loginsession/"
DEFAULT_AUTH_BARRIER_LOGINSESSION_CASSANDRA_KEYSPACE = "conductor_auth"
DEFAULT_AUTH_BARRIER_LOGINSESSION_CASSANDRA_TABLE = "loginsession"
AUTH_BARRIER_LOGIN_PAGE_MARKERS = (
    "id=\"loginForm\"",
    "Log In | Puppet Enterprise",
    "/auth/scripts/lib/login.js",
)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def log(message):
    print(f"[relay] {message}", flush=True)


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def env_float(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return float(value)


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


def normalize_cassandra_identifier(value, field_name):
    identifier = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", identifier):
        raise RuntimeError(f"invalid Cassandra {field_name}: {value!r}")
    return identifier


def normalize_command_name(value):
    return re.sub(r"\s+", " ", value.replace("_", " ").replace("-", " ").strip().lower())


def b64decode_text(value):
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def stable_json(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def sql_identifier(value):
    identifier = (value or "").strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", identifier):
        raise RuntimeError(f"invalid SQL identifier: {value!r}")
    return f'"{identifier}"'


def pod_ordinal(value):
    match = re.search(r"-(\d+)$", (value or "").strip())
    return int(match.group(1)) if match else 0


def sequence_value_for_residue(minimum_value, stride, residue):
    stride = max(int(stride or 1), 1)
    residue = max(int(residue or 1), 1)
    minimum = max(int(minimum_value or 0), residue)
    steps = max(0, (minimum - residue + stride - 1) // stride)
    return residue + steps * stride


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


def datetime_to_text(value):
    value = normalize_datetime(value)
    return value.isoformat() if value is not None else ""


def parse_datetime_text(value):
    text = (value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    return normalize_datetime(datetime.fromisoformat(normalized))


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


def normalize_classifier_group(group):
    normalized = {
        "id": ((group or {}).get("id") or "").strip(),
        "name": ((group or {}).get("name") or "").strip(),
        "parent": ((group or {}).get("parent") or "").strip(),
        "environment": ((group or {}).get("environment") or "production").strip() or "production",
        "environment_trumps": bool((group or {}).get("environment_trumps", False)),
        "description": (group or {}).get("description") or "",
        "classes": dict((group or {}).get("classes") or {}),
        "variables": dict((group or {}).get("variables") or {}),
    }
    if "rule" in (group or {}):
        normalized["rule"] = (group or {}).get("rule")
    else:
        normalized["rule"] = None
    config_data = dict((group or {}).get("config_data") or {})
    if config_data:
        normalized["config_data"] = config_data
    return normalized


def classifier_group_put_payload(group):
    normalized = normalize_classifier_group(group)
    payload = {
        "name": normalized["name"],
        "parent": normalized["parent"],
        "environment": normalized["environment"],
        "environment_trumps": normalized["environment_trumps"],
        "description": normalized["description"],
        "classes": normalized["classes"],
    }
    if normalized.get("rule", None) is not None:
        payload["rule"] = normalized["rule"]
    if normalized["variables"]:
        payload["variables"] = normalized["variables"]
    if normalized.get("config_data"):
        payload["config_data"] = normalized["config_data"]
    return payload


def classifier_group_sort_key(group):
    return (
        (group.get("parent") or "").strip(),
        (group.get("name") or "").strip(),
        (group.get("id") or "").strip(),
    )


def classifier_state_hash(groups):
    normalized = []
    for group in groups or []:
        entry = normalize_classifier_group(group)
        entry["id"] = ((group or {}).get("id") or entry["id"] or "").strip()
        entry["parent"] = ((group or {}).get("parent") or entry["parent"] or "").strip()
        normalized.append(entry)
    normalized.sort(key=classifier_group_sort_key)
    return sha256_text(stable_json({"groups": normalized}))


def classifier_group_depth(group_map, group_id, root_id, cache):
    group_id = (group_id or "").strip()
    if not group_id:
        return 0
    if group_id in cache:
        return cache[group_id]
    if group_id == root_id:
        cache[group_id] = 0
        return 0
    group = group_map.get(group_id)
    if group is None:
        cache[group_id] = 0
        return 0
    parent_id = (group.get("parent") or "").strip()
    if not parent_id or parent_id == group_id:
        cache[group_id] = 1
        return 1
    depth = classifier_group_depth(group_map, parent_id, root_id, cache) + 1
    cache[group_id] = depth
    return depth

def request_path_with_query(parsed):
    path = parsed.path or "/"
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def extract_response_cookie(headers, cookie_name):
    prefix = f"{cookie_name}="
    for header_name, header_value in headers or []:
        if header_name.lower() != "set-cookie":
            continue
        value = (header_value or "").strip()
        if value.startswith(prefix):
            return value.split(";", 1)[0]
    return ""


def extract_request_cookie(headers, cookie_name):
    cookie_header = ""
    if hasattr(headers, "get"):
        cookie_header = headers.get("Cookie", "")
    elif isinstance(headers, dict):
        cookie_header = headers.get("Cookie", "")
    for item in (cookie_header or "").split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name == cookie_name:
            return value.strip()
    return ""


def extract_request_auth_token(headers):
    if hasattr(headers, "get"):
        x_authentication = (headers.get("X-Authentication", "") or "").strip()
        authorization = (headers.get("Authorization", "") or "").strip()
    elif isinstance(headers, dict):
        x_authentication = (headers.get("X-Authentication", "") or "").strip()
        authorization = (headers.get("Authorization", "") or "").strip()
    else:
        x_authentication = ""
        authorization = ""
    if x_authentication:
        return x_authentication
    bearer_prefix = "Bearer "
    if authorization.startswith(bearer_prefix):
        return authorization[len(bearer_prefix):].strip()
    return ""


def response_is_console_page(body):
    if not body:
        return False
    text = body[:8192].decode("utf-8", errors="replace")
    return not any(marker in text for marker in AUTH_BARRIER_LOGIN_PAGE_MARKERS)


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


def http_request_json(method, url, headers=None, payload=None, context=None, timeout=30):
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return http_request_raw(
        method,
        url,
        headers=headers,
        data=data,
        context=context,
        timeout=timeout,
    )


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


def relay_status_ready(status):
    if not status.get("connected", False):
        return False
    if not status.get("participantReady", False):
        return False
    if not status.get("localPuppetdbHealthy", False):
        return False
    if status.get("commandProxyEnabled", False) and not status.get("commandProxyReady", False):
        return False
    if status.get("caSyncEnabled", False) and not status.get("caSyncReady", False):
        return False
    if status.get("classifierSyncEnabled", False) and not status.get("classifierSyncReady", False):
        return False
    if status.get("rbacSyncEnabled", False) and not status.get("rbacSyncReady", False):
        return False
    if status.get("rbacTokenSyncEnabled", False) and not status.get("rbacTokenSyncReady", False):
        return False
    if status.get("orchestrationSyncEnabled", False) and not status.get("orchestrationSyncReady", False):
        return False
    if status.get("inventorySyncEnabled", False) and not status.get("inventorySyncReady", False):
        return False
    if status.get("authBarrierEnabled", False) and not status.get("authBarrierReady", False):
        return False
    if status.get("codeDeployEnabled", False) and not status.get("codeDeployReady", False):
        return False
    return True


def relay_frontdoor_blockers(status):
    blockers = []
    if not status.get("connected", False):
        blockers.append("relay-disconnected")
    if not status.get("participantReady", False):
        blockers.append("participant-not-ready")
    if not status.get("localPuppetdbHealthy", False):
        blockers.append("local-puppetdb-unhealthy")
    if status.get("commandProxyEnabled", False) and not status.get("commandProxyReady", False):
        blockers.append("command-proxy-not-ready")
    if status.get("caSyncEnabled", False) and not status.get("caSyncReady", False):
        blockers.append("ca-sync-not-ready")
    if status.get("classifierSyncEnabled", False) and not status.get("classifierSyncReady", False):
        blockers.append("classifier-sync-not-ready")
    if status.get("rbacSyncEnabled", False) and not status.get("rbacSyncReady", False):
        blockers.append("rbac-sync-not-ready")
    if status.get("rbacTokenSyncEnabled", False) and not status.get("rbacTokenSyncReady", False):
        blockers.append("rbac-token-sync-not-ready")
    if status.get("orchestrationSyncEnabled", False) and not status.get("orchestrationSyncReady", False):
        blockers.append("orchestration-sync-not-ready")
    if status.get("inventorySyncEnabled", False) and not status.get("inventorySyncReady", False):
        blockers.append("inventory-sync-not-ready")
    if status.get("authBarrierEnabled", False) and not status.get("authBarrierReady", False):
        blockers.append("auth-barrier-not-ready")
    if status.get("codeDeployEnabled", False) and not status.get("codeDeployReady", False):
        blockers.append("code-deploy-not-ready")
    return blockers


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

    if not relay_status_ready(status):
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


class LocalCodeDeployHookHandler(BaseHTTPRequestHandler):
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
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise RelayLocalCommandError(400, "invalid content length") from error
        return self.rfile.read(content_length) if content_length > 0 else b""

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != self.server.runtime.code_deploy_hook_path:
            self.text_response(404, "unknown relay endpoint")
            return

        try:
            body = self.read_request_body()
            payload = json.loads(body.decode("utf-8"))
            response = self.server.runtime.handle_local_code_deploy_hook(payload)
        except json.JSONDecodeError:
            self.text_response(400, "invalid JSON body")
            return
        except RelayLocalCommandError as error:
            self.text_response(error.status_code, error.message)
            return
        except Exception as error:  # pragma: no cover - defensive fallback
            self.text_response(500, str(error))
            return

        self.json_response(200, response)


class LocalAuthBarrierHandler(BaseHTTPRequestHandler):
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
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_response(self, status_code, payload):
        body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_proxy_response(self, status_code, headers, body):
        body = body or b""
        self.send_response(status_code)
        for header_name, header_value in headers or []:
            header_lower = header_name.lower()
            if header_lower in HOP_BY_HOP_HEADERS or header_lower == "content-length":
                continue
            self.send_header(header_name, header_value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
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

    def peer_subject(self):
        if not hasattr(self.connection, "getpeercert"):
            return ""
        try:
            peer_cert = self.connection.getpeercert(binary_form=True)
        except TypeError:
            return ""
        if not peer_cert:
            return ""
        try:
            certificate = x509.load_der_x509_certificate(peer_cert)
            return certificate.subject.rfc4514_string()
        except Exception:
            return ""

    def handle_loginsession_request(self, parsed):
        session_id = parsed.path[len(DEFAULT_AUTH_BARRIER_LOGINSESSION_PATH_PREFIX) :].strip()
        try:
            session_id = self.server.runtime.normalize_loginsession_id(session_id)
        except RelayLocalCommandError as error:
            self.text_response(error.status_code, error.message)
            return

        try:
            if self.command == "GET":
                if self.server.runtime.auth_barrier_loginsession_uses_cassandra():
                    payload = self.server.runtime.read_shared_loginsession(session_id)
                else:
                    payload = self.server.runtime.read_local_loginsession(session_id)
                if payload is None:
                    self.text_response(404, "loginsession not found")
                    return
                self.json_response(200, payload)
                return

            if self.command == "PUT":
                body = self.read_request_body()
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.text_response(400, "invalid JSON body")
                    return
                if self.server.runtime.auth_barrier_loginsession_uses_cassandra():
                    self.server.runtime.upsert_shared_loginsession(
                        payload,
                        expected_session_id=session_id,
                    )
                else:
                    self.server.runtime.upsert_local_loginsession(
                        payload,
                        expected_session_id=session_id,
                    )
                self.json_response(200, {"id": session_id, "status": "ok"})
                return

            self.text_response(405, "method not allowed")
        except RelayLocalCommandError as error:
            self.text_response(error.status_code, error.message)
        except Exception as error:  # pragma: no cover - defensive fallback
            self.text_response(500, str(error))

    def handle_proxy_request(self):
        parsed = urllib.parse.urlsplit(self.path)
        if (
            self.server.barrier_name == "api"
            and parsed.path.startswith(DEFAULT_AUTH_BARRIER_LOGINSESSION_PATH_PREFIX)
        ):
            self.handle_loginsession_request(parsed)
            return
        client_address = self.client_address[0] if self.client_address else ""
        try:
            body = self.read_request_body()
            status_code, headers, response_body = self.server.runtime.handle_local_auth_barrier_request(
                self.server.barrier_name,
                self.command,
                parsed,
                self.headers,
                body,
                peer_subject=self.peer_subject(),
                client_address=client_address,
            )
        except RelayLocalCommandError as error:
            self.text_response(error.status_code, error.message)
            return
        except Exception as error:  # pragma: no cover - defensive fallback
            self.text_response(500, str(error))
            return

        self.send_proxy_response(status_code, headers, response_body)

    def do_DELETE(self):
        self.handle_proxy_request()

    def do_GET(self):
        self.handle_proxy_request()

    def do_HEAD(self):
        self.handle_proxy_request()

    def do_OPTIONS(self):
        self.handle_proxy_request()

    def do_PATCH(self):
        self.handle_proxy_request()

    def do_POST(self):
        self.handle_proxy_request()

    def do_PUT(self):
        self.handle_proxy_request()


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

    def request(self, method, path, payload=None, expected=None, content_type="application/json"):
        headers = dict(self.headers)
        if payload is not None:
            headers["Content-Type"] = content_type
        status, body = http_request_json(
            method,
            f"{self.base_url}{path}",
            headers=headers,
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

    def get_pod(self, pod_name, namespace=None):
        namespace = namespace or self.namespace
        path = f"/api/v1/namespaces/{namespace}/pods/{pod_name}"
        status, data = self.request("GET", path, expected={200, 404})
        return data if status == 200 else None

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
            or "127.0.0.1"
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
        self.code_deploy_enabled = env_bool("CONDUCTOR_RELAY_CODE_DEPLOY_ENABLED", False)
        self.code_deploy_service_host = (
            os.environ.get("CONDUCTOR_RELAY_CODE_DEPLOY_SERVICE_HOST", "").strip()
            or "pe"
        )
        self.code_deploy_hook_listen_host = (
            os.environ.get("CONDUCTOR_RELAY_CODE_DEPLOY_HOOK_LISTEN_HOST", "").strip()
            or "0.0.0.0"
        )
        self.code_deploy_hook_listen_port = env_int(
            "CONDUCTOR_RELAY_CODE_DEPLOY_HOOK_LISTEN_PORT",
            18082,
        )
        self.code_deploy_hook_path = (
            os.environ.get("CONDUCTOR_RELAY_CODE_DEPLOY_HOOK_PATH", "").strip()
            or DEFAULT_CODE_DEPLOY_HOOK_PATH
        )
        self.code_deploy_target_roles = env_csv(
            "CONDUCTOR_RELAY_CODE_DEPLOY_TARGET_ROLES",
            default=DEFAULT_CODE_DEPLOY_TARGET_ROLES,
        )
        self.code_deploy_username = (
            os.environ.get("CONDUCTOR_RELAY_CODE_DEPLOY_USERNAME", "").strip()
            or "admin"
        )
        self.code_deploy_password = os.environ.get("CONDUCTOR_RELAY_CODE_DEPLOY_PASSWORD", "")
        self.code_deploy_token_lifetime = (
            os.environ.get("CONDUCTOR_RELAY_CODE_DEPLOY_TOKEN_LIFETIME", "").strip()
            or "15m"
        )
        self.code_deploy_token_label = (
            os.environ.get("CONDUCTOR_RELAY_CODE_DEPLOY_TOKEN_LABEL", "").strip()
            or "pe-k8s-conductor-code-deploy"
        )
        self.code_deploy_request_timeout_seconds = env_int(
            "CONDUCTOR_RELAY_CODE_DEPLOY_REQUEST_TIMEOUT_SECONDS",
            900,
        )
        self.code_deploy_status_poll_interval = env_int(
            "CONDUCTOR_RELAY_CODE_DEPLOY_STATUS_POLL_INTERVAL_SECONDS",
            60,
        )
        self.code_deploy_state_path = os.path.join(self.output_dir, DEFAULT_CODE_DEPLOY_STATE_FILENAME)
        self.classifier_sync_enabled = env_bool("CONDUCTOR_RELAY_CLASSIFIER_SYNC_ENABLED", False)
        self.classifier_sync_service_host = (
            os.environ.get("CONDUCTOR_RELAY_CLASSIFIER_SYNC_SERVICE_HOST", "").strip()
            or self.code_deploy_service_host
        )
        self.classifier_sync_username = (
            os.environ.get("CONDUCTOR_RELAY_CLASSIFIER_SYNC_USERNAME", "").strip()
            or self.code_deploy_username
        )
        self.classifier_sync_password = (
            os.environ.get("CONDUCTOR_RELAY_CLASSIFIER_SYNC_PASSWORD", "")
            or self.code_deploy_password
        )
        self.classifier_sync_token_lifetime = (
            os.environ.get("CONDUCTOR_RELAY_CLASSIFIER_SYNC_TOKEN_LIFETIME", "").strip()
            or self.code_deploy_token_lifetime
        )
        self.classifier_sync_token_label = (
            os.environ.get("CONDUCTOR_RELAY_CLASSIFIER_SYNC_TOKEN_LABEL", "").strip()
            or "pe-k8s-conductor-classifier-sync"
        )
        self.classifier_sync_target_roles = env_csv(
            "CONDUCTOR_RELAY_CLASSIFIER_SYNC_TARGET_ROLES",
            default=DEFAULT_CLASSIFIER_TARGET_ROLES,
        )
        self.classifier_sync_scope = DEFAULT_CLASSIFIER_SYNC_SCOPE
        self.classifier_sync_backend = "cassandra" if self.classifier_sync_enabled else ""
        self.classifier_sync_state_path = os.path.join(
            self.output_dir,
            DEFAULT_CLASSIFIER_SYNC_STATE_FILENAME,
        )
        self.classifier_sync_cassandra_contact_points = env_csv(
            "CONDUCTOR_RELAY_CLASSIFIER_SYNC_CASSANDRA_CONTACT_POINTS",
        )
        self.classifier_sync_cassandra_port = env_int(
            "CONDUCTOR_RELAY_CLASSIFIER_SYNC_CASSANDRA_PORT",
            9042,
        )
        self.classifier_sync_cassandra_keyspace = normalize_cassandra_identifier(
            os.environ.get("CONDUCTOR_RELAY_CLASSIFIER_SYNC_CASSANDRA_KEYSPACE", "").strip()
            or DEFAULT_CLASSIFIER_SYNC_CASSANDRA_KEYSPACE,
            "keyspace",
        )
        self.classifier_sync_cassandra_table = normalize_cassandra_identifier(
            os.environ.get("CONDUCTOR_RELAY_CLASSIFIER_SYNC_CASSANDRA_TABLE", "").strip()
            or DEFAULT_CLASSIFIER_SYNC_CASSANDRA_TABLE,
            "table",
        )
        self.classifier_sync_cassandra_replication_factor = env_int(
            "CONDUCTOR_RELAY_CLASSIFIER_SYNC_CASSANDRA_REPLICATION_FACTOR",
            3,
        )
        self.rbac_sync_enabled = env_bool("CONDUCTOR_RELAY_RBAC_SYNC_ENABLED", False)
        self.rbac_sync_target_roles = env_csv(
            "CONDUCTOR_RELAY_RBAC_SYNC_TARGET_ROLES",
            default=DEFAULT_RBAC_TARGET_ROLES,
        )
        self.rbac_sync_scope = DEFAULT_RBAC_SYNC_SCOPE
        self.rbac_sync_backend = "cassandra" if self.rbac_sync_enabled else ""
        self.rbac_sync_state_path = os.path.join(
            self.output_dir,
            DEFAULT_RBAC_SYNC_STATE_FILENAME,
        )
        self.rbac_sync_rbac_conf_path = (
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_RBAC_CONF_PATH", "").strip()
            or DEFAULT_RBAC_SYNC_RBAC_CONF_PATH
        )
        self.rbac_sync_rbac_database_conf_path = (
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_RBAC_DATABASE_CONF_PATH", "").strip()
            or DEFAULT_RBAC_SYNC_RBAC_DATABASE_CONF_PATH
        )
        self.rbac_sync_keys_path = (
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_KEYS_PATH", "").strip()
            or DEFAULT_RBAC_SYNC_KEYS_PATH
        )
        self.rbac_sync_shared_secret_dir = (
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_SHARED_SECRET_DIR", "").strip()
            or DEFAULT_RBAC_SYNC_SHARED_SECRET_DIR
        )
        self.rbac_sync_shared_token_private_key_path = (
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_SHARED_TOKEN_PRIVATE_KEY_PATH", "").strip()
            or DEFAULT_RBAC_SYNC_SHARED_TOKEN_PRIVATE_KEY_PATH
        )
        self.rbac_sync_shared_token_public_key_path = (
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_SHARED_TOKEN_PUBLIC_KEY_PATH", "").strip()
            or DEFAULT_RBAC_SYNC_SHARED_TOKEN_PUBLIC_KEY_PATH
        )
        self.rbac_sync_shared_saml_key_path = (
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_SHARED_SAML_KEY_PATH", "").strip()
            or DEFAULT_RBAC_SYNC_SHARED_SAML_KEY_PATH
        )
        self.rbac_sync_shared_saml_cert_path = (
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_SHARED_SAML_CERT_PATH", "").strip()
            or DEFAULT_RBAC_SYNC_SHARED_SAML_CERT_PATH
        )
        self.rbac_sync_excluded_token_label_prefixes = env_csv(
            "CONDUCTOR_RELAY_RBAC_SYNC_EXCLUDED_TOKEN_LABEL_PREFIXES",
            default=DEFAULT_RBAC_SYNC_EXCLUDED_TOKEN_LABEL_PREFIXES,
        )
        self.rbac_sync_cassandra_contact_points = env_csv(
            "CONDUCTOR_RELAY_RBAC_SYNC_CASSANDRA_CONTACT_POINTS",
        )
        self.rbac_sync_cassandra_port = env_int(
            "CONDUCTOR_RELAY_RBAC_SYNC_CASSANDRA_PORT",
            9042,
        )
        self.rbac_sync_cassandra_keyspace = normalize_cassandra_identifier(
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_CASSANDRA_KEYSPACE", "").strip()
            or DEFAULT_RBAC_SYNC_CASSANDRA_KEYSPACE,
            "keyspace",
        )
        self.rbac_sync_cassandra_table = normalize_cassandra_identifier(
            os.environ.get("CONDUCTOR_RELAY_RBAC_SYNC_CASSANDRA_TABLE", "").strip()
            or DEFAULT_RBAC_SYNC_CASSANDRA_TABLE,
            "table",
        )
        self.rbac_sync_cassandra_replication_factor = env_int(
            "CONDUCTOR_RELAY_RBAC_SYNC_CASSANDRA_REPLICATION_FACTOR",
            3,
        )
        self.rbac_token_sync_enabled = env_bool(
            "CONDUCTOR_RELAY_RBAC_TOKEN_SYNC_ENABLED",
            False,
        )
        self.rbac_token_sync_target_roles = env_csv(
            "CONDUCTOR_RELAY_RBAC_TOKEN_SYNC_TARGET_ROLES",
            default=DEFAULT_RBAC_TARGET_ROLES,
        )
        self.rbac_token_sync_scope = DEFAULT_RBAC_TOKEN_SYNC_SCOPE
        self.rbac_token_sync_state_path = os.path.join(
            self.output_dir,
            DEFAULT_RBAC_TOKEN_SYNC_STATE_FILENAME,
        )
        self.rbac_token_sync_cassandra_contact_points = env_csv(
            "CONDUCTOR_RELAY_RBAC_TOKEN_SYNC_CASSANDRA_CONTACT_POINTS",
        )
        self.rbac_token_sync_cassandra_port = env_int(
            "CONDUCTOR_RELAY_RBAC_TOKEN_SYNC_CASSANDRA_PORT",
            9042,
        )
        self.rbac_token_sync_cassandra_keyspace = normalize_cassandra_identifier(
            os.environ.get("CONDUCTOR_RELAY_RBAC_TOKEN_SYNC_CASSANDRA_KEYSPACE", "").strip()
            or DEFAULT_RBAC_TOKEN_SYNC_CASSANDRA_KEYSPACE,
            "keyspace",
        )
        self.rbac_token_sync_cassandra_table = normalize_cassandra_identifier(
            os.environ.get("CONDUCTOR_RELAY_RBAC_TOKEN_SYNC_CASSANDRA_TABLE", "").strip()
            or DEFAULT_RBAC_TOKEN_SYNC_CASSANDRA_TABLE,
            "table",
        )
        self.rbac_token_sync_cassandra_replication_factor = env_int(
            "CONDUCTOR_RELAY_RBAC_TOKEN_SYNC_CASSANDRA_REPLICATION_FACTOR",
            3,
        )
        self.orchestration_sync_enabled = env_bool(
            "CONDUCTOR_RELAY_ORCHESTRATION_SYNC_ENABLED",
            False,
        )
        self.orchestration_sync_target_roles = env_csv(
            "CONDUCTOR_RELAY_ORCHESTRATION_SYNC_TARGET_ROLES",
            default=DEFAULT_ORCHESTRATION_TARGET_ROLES,
        )
        self.orchestration_sync_scope = DEFAULT_ORCHESTRATION_SYNC_SCOPE
        self.orchestration_sync_backend = "cassandra" if self.orchestration_sync_enabled else ""
        self.orchestration_sync_state_path = os.path.join(
            self.output_dir,
            DEFAULT_ORCHESTRATION_SYNC_STATE_FILENAME,
        )
        self.orchestration_sync_orchestrator_conf_path = (
            os.environ.get("CONDUCTOR_RELAY_ORCHESTRATION_SYNC_ORCHESTRATOR_CONF_PATH", "").strip()
            or DEFAULT_ORCHESTRATION_SYNC_ORCHESTRATOR_CONF_PATH
        )
        self.orchestration_sync_inventory_conf_path = (
            os.environ.get("CONDUCTOR_RELAY_ORCHESTRATION_SYNC_INVENTORY_CONF_PATH", "").strip()
            or DEFAULT_ORCHESTRATION_SYNC_INVENTORY_CONF_PATH
        )
        self.orchestration_sync_sequence_stride = env_int(
            "CONDUCTOR_RELAY_ORCHESTRATION_SYNC_SEQUENCE_STRIDE",
            DEFAULT_ORCHESTRATION_SYNC_SEQUENCE_STRIDE,
        )
        self.orchestration_sync_cassandra_contact_points = env_csv(
            "CONDUCTOR_RELAY_ORCHESTRATION_SYNC_CASSANDRA_CONTACT_POINTS",
        )
        self.orchestration_sync_cassandra_port = env_int(
            "CONDUCTOR_RELAY_ORCHESTRATION_SYNC_CASSANDRA_PORT",
            9042,
        )
        self.orchestration_sync_cassandra_keyspace = normalize_cassandra_identifier(
            os.environ.get("CONDUCTOR_RELAY_ORCHESTRATION_SYNC_CASSANDRA_KEYSPACE", "").strip()
            or DEFAULT_ORCHESTRATION_SYNC_CASSANDRA_KEYSPACE,
            "keyspace",
        )
        self.orchestration_sync_cassandra_table = normalize_cassandra_identifier(
            os.environ.get("CONDUCTOR_RELAY_ORCHESTRATION_SYNC_CASSANDRA_TABLE", "").strip()
            or DEFAULT_ORCHESTRATION_SYNC_CASSANDRA_TABLE,
            "table",
        )
        self.orchestration_sync_cassandra_replication_factor = env_int(
            "CONDUCTOR_RELAY_ORCHESTRATION_SYNC_CASSANDRA_REPLICATION_FACTOR",
            3,
        )
        self.orchestration_sync_sequence_residue = pod_ordinal(self.pod_name) + 1
        if self.orchestration_sync_sequence_stride < self.orchestration_sync_sequence_residue:
            raise RuntimeError(
                "orchestration sequence stride must be greater than or equal to the pod residue"
            )
        self.inventory_sync_enabled = env_bool(
            "CONDUCTOR_RELAY_INVENTORY_SYNC_ENABLED",
            False,
        )
        self.inventory_sync_target_roles = env_csv(
            "CONDUCTOR_RELAY_INVENTORY_SYNC_TARGET_ROLES",
            default=DEFAULT_ORCHESTRATION_TARGET_ROLES,
        )
        self.inventory_sync_scope = DEFAULT_INVENTORY_SYNC_SCOPE
        self.inventory_sync_state_path = os.path.join(
            self.output_dir,
            DEFAULT_INVENTORY_SYNC_STATE_FILENAME,
        )
        self.inventory_sync_inventory_conf_path = (
            os.environ.get("CONDUCTOR_RELAY_INVENTORY_SYNC_INVENTORY_CONF_PATH", "").strip()
            or DEFAULT_ORCHESTRATION_SYNC_INVENTORY_CONF_PATH
        )
        self.inventory_sync_cassandra_contact_points = env_csv(
            "CONDUCTOR_RELAY_INVENTORY_SYNC_CASSANDRA_CONTACT_POINTS",
        )
        self.inventory_sync_cassandra_port = env_int(
            "CONDUCTOR_RELAY_INVENTORY_SYNC_CASSANDRA_PORT",
            9042,
        )
        self.inventory_sync_cassandra_keyspace = normalize_cassandra_identifier(
            os.environ.get(
                "CONDUCTOR_RELAY_INVENTORY_SYNC_CASSANDRA_KEYSPACE",
                "",
            ).strip()
            or DEFAULT_INVENTORY_SYNC_CASSANDRA_KEYSPACE,
            "keyspace",
        )
        self.inventory_sync_cassandra_table = normalize_cassandra_identifier(
            os.environ.get(
                "CONDUCTOR_RELAY_INVENTORY_SYNC_CASSANDRA_TABLE",
                "",
            ).strip()
            or DEFAULT_INVENTORY_SYNC_CASSANDRA_TABLE,
            "table",
        )
        self.inventory_sync_cassandra_replication_factor = env_int(
            "CONDUCTOR_RELAY_INVENTORY_SYNC_CASSANDRA_REPLICATION_FACTOR",
            3,
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
        self.auth_barrier_enabled = env_bool(
            "CONDUCTOR_RELAY_AUTH_BARRIER_ENABLED",
            self.rbac_sync_enabled,
        )
        self.auth_barrier_wait_timeout_seconds = env_int(
            "CONDUCTOR_RELAY_AUTH_BARRIER_WAIT_TIMEOUT_SECONDS",
            20,
        )
        self.auth_barrier_loginsession_backend = "cassandra" if self.auth_barrier_enabled else ""
        self.auth_barrier_loginsession_cassandra_contact_points = env_csv(
            "CONDUCTOR_RELAY_AUTH_BARRIER_LOGINSESSION_CASSANDRA_CONTACT_POINTS",
        )
        self.auth_barrier_loginsession_cassandra_port = env_int(
            "CONDUCTOR_RELAY_AUTH_BARRIER_LOGINSESSION_CASSANDRA_PORT",
            9042,
        )
        self.auth_barrier_loginsession_cassandra_keyspace = (
            normalize_cassandra_identifier(
                os.environ.get(
                    "CONDUCTOR_RELAY_AUTH_BARRIER_LOGINSESSION_CASSANDRA_KEYSPACE",
                    "",
                ).strip()
                or DEFAULT_AUTH_BARRIER_LOGINSESSION_CASSANDRA_KEYSPACE,
                "keyspace",
            )
        )
        self.auth_barrier_loginsession_cassandra_table = (
            normalize_cassandra_identifier(
                os.environ.get(
                    "CONDUCTOR_RELAY_AUTH_BARRIER_LOGINSESSION_CASSANDRA_TABLE",
                    "",
                ).strip()
                or DEFAULT_AUTH_BARRIER_LOGINSESSION_CASSANDRA_TABLE,
                "table",
            )
        )
        self.auth_barrier_loginsession_cassandra_replication_factor = env_int(
            "CONDUCTOR_RELAY_AUTH_BARRIER_LOGINSESSION_CASSANDRA_REPLICATION_FACTOR",
            3,
        )
        self.auth_barrier_request_timeout_seconds = env_int(
            "CONDUCTOR_RELAY_AUTH_BARRIER_REQUEST_TIMEOUT_SECONDS",
            30,
        )
        self.auth_barrier_peer_request_timeout_seconds = env_int(
            "CONDUCTOR_RELAY_AUTH_BARRIER_PEER_REQUEST_TIMEOUT_SECONDS",
            5,
        )
        self.auth_barrier_poll_interval_seconds = env_float(
            "CONDUCTOR_RELAY_AUTH_BARRIER_POLL_INTERVAL_SECONDS",
            0.25,
        )
        self.auth_barrier_http_listen_host = (
            os.environ.get("CONDUCTOR_RELAY_AUTH_BARRIER_HTTP_LISTEN_HOST", "").strip()
            or "127.0.0.1"
        )
        self.auth_barrier_http_port = env_int(
            "CONDUCTOR_RELAY_AUTH_BARRIER_HTTP_LISTEN_PORT",
            4430,
        )
        self.auth_barrier_http_target_host = (
            os.environ.get("CONDUCTOR_RELAY_AUTH_BARRIER_HTTP_TARGET_HOST", "").strip()
            or "127.0.0.1"
        )
        self.auth_barrier_http_target_port = env_int(
            "CONDUCTOR_RELAY_AUTH_BARRIER_HTTP_TARGET_PORT",
            4440,
        )
        self.auth_barrier_api_listen_host = (
            os.environ.get("CONDUCTOR_RELAY_AUTH_BARRIER_API_LISTEN_HOST", "").strip()
            or "0.0.0.0"
        )
        self.auth_barrier_api_port = env_int(
            "CONDUCTOR_RELAY_AUTH_BARRIER_API_LISTEN_PORT",
            4444,
        )
        self.auth_barrier_api_target_host = (
            os.environ.get("CONDUCTOR_RELAY_AUTH_BARRIER_API_TARGET_HOST", "").strip()
            or "127.0.0.1"
        )
        self.auth_barrier_api_target_port = env_int(
            "CONDUCTOR_RELAY_AUTH_BARRIER_API_TARGET_PORT",
            4432,
        )
        self.auth_barrier_control_plane_headless_service = (
            os.environ.get("CONDUCTOR_RELAY_CONTROL_PLANE_HEADLESS_SERVICE", "").strip()
            or f"{self.code_deploy_service_host}-headless"
        )
        self.console_webserver_conf_path = (
            os.environ.get("CONDUCTOR_RELAY_CONSOLE_WEBSERVER_CONF_PATH", "").strip()
            or DEFAULT_CONSOLE_WEBSERVER_CONF_PATH
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
        self.code_deploy_hook_start_error = ""
        self.code_deploy_runtime_error = ""
        self.classifier_sync_runtime_error = ""
        self.rbac_sync_runtime_error = ""
        self.rbac_token_sync_runtime_error = ""
        self.orchestration_sync_runtime_error = ""
        self.inventory_sync_runtime_error = ""
        self.code_deploy_suppressions = {}
        self.code_deploy_queue = queue.Queue()
        self.code_deploy_queued = set()
        self.code_deploy_worker_thread = None
        self.code_deploy_states = {}
        self.last_code_deploy_status_poll = 0
        self.classifier_sync_state = self.default_classifier_sync_state()
        self.next_classifier_publish = 0
        self.last_published_classifier_hash = ""
        self.rbac_sync_state = self.default_rbac_sync_state()
        self.next_rbac_publish = 0
        self.last_published_rbac_hash = ""
        self.rbac_token_sync_state = self.default_rbac_token_sync_state()
        self.next_rbac_token_publish = 0
        self.last_published_rbac_token_hash = ""
        self.orchestration_sync_state = self.default_orchestration_sync_state()
        self.next_orchestration_publish = 0
        self.last_published_orchestration_hash = ""
        self.inventory_sync_state = self.default_inventory_sync_state()
        self.next_inventory_publish = 0
        self.last_published_inventory_hash = ""

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
        self.classifier_sync_cassandra_cluster = None
        self.classifier_sync_cassandra_session = None
        self.rbac_sync_cassandra_cluster = None
        self.rbac_sync_cassandra_session = None
        self.rbac_token_sync_cassandra_cluster = None
        self.rbac_token_sync_cassandra_session = None
        self.orchestration_sync_cassandra_cluster = None
        self.orchestration_sync_cassandra_session = None
        self.inventory_sync_cassandra_cluster = None
        self.inventory_sync_cassandra_session = None
        self.code_deploy_hook_server = None
        self.code_deploy_hook_thread = None
        self.auth_barrier_http_server = None
        self.auth_barrier_http_thread = None
        self.auth_barrier_api_server = None
        self.auth_barrier_api_thread = None
        self.auth_barrier_start_error = ""
        self.auth_barrier_last_error = ""
        self.auth_barrier_loginsession_cassandra_cluster = None
        self.auth_barrier_loginsession_cassandra_session = None
        self.frontdoor_status_enabled = env_bool(
            "CONDUCTOR_RELAY_FRONTDOOR_STATUS_ENABLED",
            self.relay_role == "control-plane",
        )
        self.frontdoor_status_publish_interval = env_int(
            "CONDUCTOR_RELAY_FRONTDOOR_STATUS_PUBLISH_INTERVAL_SECONDS",
            5,
        )
        self.next_frontdoor_status_publish = 0
        self.last_frontdoor_status_annotations = None
        self.frontdoor_status_publish_error = ""

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
            "codeDeployEnabled": self.code_deploy_enabled,
            "codeDeployHookReady": not self.code_deploy_enabled,
            "codeDeployReady": not self.code_deploy_enabled,
            "codeDeployHookUrl": "",
            "codeDeployTargetRoles": list(self.code_deploy_target_roles),
            "codeDeployEnvironmentCount": 0,
            "codeDeployConvergedCount": 0,
            "codeDeployPendingCount": 0,
            "codeDeployFailureCount": 0,
            "codeDeployLastPublishedAt": 0,
            "codeDeployLastConvergedAt": 0,
            "codeDeployLastError": "",
            "codeDeployEnvironments": {},
            "classifierSyncEnabled": self.classifier_sync_enabled,
            "classifierSyncReady": not self.classifier_sync_enabled,
            "classifierSyncBackend": self.classifier_sync_backend if self.classifier_sync_enabled else "",
            "classifierSyncServiceHost": self.classifier_sync_service_host if self.classifier_sync_enabled else "",
            "classifierSyncTargetRoles": list(self.classifier_sync_target_roles),
            "classifierSyncScope": self.classifier_sync_scope if self.classifier_sync_enabled else "",
            "classifierSyncExcludedRoots": sorted(CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES),
            "classifierSyncState": "idle",
            "classifierSyncDesiredHash": "",
            "classifierSyncActualHash": "",
            "classifierSyncDesiredGroupCount": 0,
            "classifierSyncActualGroupCount": 0,
            "classifierSyncLastPublishedAt": 0,
            "classifierSyncLastAppliedAt": 0,
            "classifierSyncLastConvergedAt": 0,
            "classifierSyncLastError": "",
            "rbacSyncEnabled": self.rbac_sync_enabled,
            "rbacSyncReady": not self.rbac_sync_enabled,
            "rbacSyncBackend": self.rbac_sync_backend if self.rbac_sync_enabled else "",
            "rbacSyncTargetRoles": list(self.rbac_sync_target_roles),
            "rbacSyncScope": self.rbac_sync_scope if self.rbac_sync_enabled else "",
            "rbacSyncExcludedTokenLabelPrefixes": list(self.rbac_sync_excluded_token_label_prefixes),
            "rbacSyncState": "idle",
            "rbacSyncDesiredHash": "",
            "rbacSyncActualHash": "",
            "rbacSyncDesiredTableCount": 0,
            "rbacSyncActualTableCount": 0,
            "rbacSyncDesiredRowCount": 0,
            "rbacSyncActualRowCount": 0,
            "rbacSyncAuthFileCount": 0,
            "rbacSyncLastPublishedAt": 0,
            "rbacSyncLastAppliedAt": 0,
            "rbacSyncLastConvergedAt": 0,
            "rbacSyncLastError": "",
            "rbacTokenSyncEnabled": self.rbac_token_sync_enabled,
            "rbacTokenSyncReady": not self.rbac_token_sync_enabled,
            "rbacTokenSyncTargetRoles": list(self.rbac_token_sync_target_roles),
            "rbacTokenSyncScope": self.rbac_token_sync_scope if self.rbac_token_sync_enabled else "",
            "rbacTokenSyncExcludedTokenLabelPrefixes": list(
                self.rbac_sync_excluded_token_label_prefixes
            ),
            "rbacTokenSyncState": "idle",
            "rbacTokenSyncDesiredHash": "",
            "rbacTokenSyncActualHash": "",
            "rbacTokenSyncDesiredTableCount": 0,
            "rbacTokenSyncActualTableCount": 0,
            "rbacTokenSyncDesiredRowCount": 0,
            "rbacTokenSyncActualRowCount": 0,
            "rbacTokenSyncAuthFileCount": 0,
            "rbacTokenSyncLastPublishedAt": 0,
            "rbacTokenSyncLastAppliedAt": 0,
            "rbacTokenSyncLastConvergedAt": 0,
            "rbacTokenSyncLastError": "",
            "orchestrationSyncEnabled": self.orchestration_sync_enabled,
            "orchestrationSyncReady": not self.orchestration_sync_enabled,
            "orchestrationSyncBackend": (
                self.orchestration_sync_backend if self.orchestration_sync_enabled else ""
            ),
            "orchestrationSyncTargetRoles": list(self.orchestration_sync_target_roles),
            "orchestrationSyncScope": self.orchestration_sync_scope if self.orchestration_sync_enabled else "",
            "orchestrationSyncState": "idle",
            "orchestrationSyncDesiredHash": "",
            "orchestrationSyncActualHash": "",
            "orchestrationSyncDesiredDatabaseCount": 0,
            "orchestrationSyncActualDatabaseCount": 0,
            "orchestrationSyncDesiredTableCount": 0,
            "orchestrationSyncActualTableCount": 0,
            "orchestrationSyncDesiredRowCount": 0,
            "orchestrationSyncActualRowCount": 0,
            "orchestrationSyncSequenceStride": (
                self.orchestration_sync_sequence_stride if self.orchestration_sync_enabled else 0
            ),
            "orchestrationSyncSequenceResidue": (
                self.orchestration_sync_sequence_residue if self.orchestration_sync_enabled else 0
            ),
            "orchestrationSyncSequenceCount": 0,
            "orchestrationSyncLastPublishedAt": 0,
            "orchestrationSyncLastAppliedAt": 0,
            "orchestrationSyncLastConvergedAt": 0,
            "orchestrationSyncLastError": "",
            "inventorySyncEnabled": self.inventory_sync_enabled,
            "inventorySyncReady": not self.inventory_sync_enabled,
            "inventorySyncTargetRoles": list(self.inventory_sync_target_roles),
            "inventorySyncScope": self.inventory_sync_scope if self.inventory_sync_enabled else "",
            "inventorySyncState": "idle",
            "inventorySyncDesiredHash": "",
            "inventorySyncActualHash": "",
            "inventorySyncDesiredTableCount": 0,
            "inventorySyncActualTableCount": 0,
            "inventorySyncDesiredRowCount": 0,
            "inventorySyncActualRowCount": 0,
            "inventorySyncAuthFileCount": 0,
            "inventorySyncLastPublishedAt": 0,
            "inventorySyncLastAppliedAt": 0,
            "inventorySyncLastConvergedAt": 0,
            "inventorySyncLastError": "",
            "authBarrierEnabled": self.auth_barrier_enabled,
            "authBarrierReady": not self.auth_barrier_enabled,
            "authBarrierHttpListenAddress": (
                f"{self.auth_barrier_http_listen_host}:{self.auth_barrier_http_port}"
                if self.auth_barrier_enabled
                else ""
            ),
            "authBarrierApiListenAddress": (
                f"{self.auth_barrier_api_listen_host}:{self.auth_barrier_api_port}"
                if self.auth_barrier_enabled
                else ""
            ),
            "authBarrierWaitTimeoutSeconds": (
                self.auth_barrier_wait_timeout_seconds if self.auth_barrier_enabled else 0
            ),
            "authBarrierLastError": "",
            "frontDoorEligible": not self.frontdoor_status_enabled,
            "frontDoorBlockers": [],
            "frontDoorReason": "disabled" if not self.frontdoor_status_enabled else "",
            "frontDoorStatusPublishedAt": 0,
            "frontDoorStatusPublishError": "",
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
        self.load_classifier_sync_state()
        self.load_rbac_sync_state()
        self.load_rbac_token_sync_state()
        self.load_orchestration_sync_state()
        self.load_inventory_sync_state()
        self.load_code_deploy_state()

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

    def refresh_frontdoor_summary(self):
        if not self.frontdoor_status_enabled:
            self.set_status(
                frontDoorEligible=True,
                frontDoorBlockers=[],
                frontDoorReason="disabled",
            )
            return

        status = self.snapshot_status()
        blockers = relay_frontdoor_blockers(status)
        eligible = not blockers
        self.set_status(
            frontDoorEligible=eligible,
            frontDoorBlockers=blockers,
            frontDoorReason="eligible" if eligible else ",".join(blockers),
        )

    def write_status(self):
        status = self.snapshot_status()
        status["lastUpdatedAt"] = int(time.time())
        with self.lock:
            self.status["lastUpdatedAt"] = status["lastUpdatedAt"]
        write_text_file(self.status_path, json.dumps(status, indent=2, sort_keys=True) + "\n")

    def publish_frontdoor_status(self, force=False):
        if not self.frontdoor_status_enabled:
            return

        now = int(time.time())
        status = self.snapshot_status()
        blockers = status.get("frontDoorBlockers") or []
        reason = (status.get("frontDoorReason") or "").strip() or (
            "eligible" if status.get("frontDoorEligible", False) else ",".join(blockers)
        )
        annotations = {
            FRONTDOOR_ELIGIBLE_ANNOTATION: "true" if status.get("frontDoorEligible", False) else "false",
            FRONTDOOR_BLOCKERS_ANNOTATION: ",".join(blockers) if blockers else "ready",
            FRONTDOOR_REASON_ANNOTATION: reason,
            FRONTDOOR_UPDATED_AT_ANNOTATION: str(now),
        }

        should_publish = (
            force
            or self.last_frontdoor_status_annotations != annotations
            or now >= self.next_frontdoor_status_publish
        )
        if not should_publish:
            return

        try:
            self.k8s.patch_pod_metadata(self.pod_name, annotations=annotations)
            self.last_frontdoor_status_annotations = dict(annotations)
            self.next_frontdoor_status_publish = now + self.frontdoor_status_publish_interval
            if self.frontdoor_status_publish_error:
                log("Front door status publication recovered")
                self.frontdoor_status_publish_error = ""
            self.set_status(
                frontDoorStatusPublishedAt=now,
                frontDoorStatusPublishError="",
            )
        except Exception as error:
            if str(error) != self.frontdoor_status_publish_error:
                log(f"Front door status publication failed: {error}")
                self.frontdoor_status_publish_error = str(error)
            self.set_status(frontDoorStatusPublishError=str(error))

    @staticmethod
    def default_code_deploy_state(environment):
        return {
            "environment": environment,
            "state": "idle",
            "desiredSignature": "",
            "actualSignature": "",
            "desiredDeployId": 0,
            "actualDeployId": 0,
            "desiredCodeCommit": "",
            "desiredEnvironmentCommit": "",
            "actualCodeCommit": "",
            "actualEnvironmentCommit": "",
            "originParticipant": "",
            "desiredPublishedAt": 0,
            "lastReceivedAt": 0,
            "lastPublishedAt": 0,
            "lastRequestedAt": 0,
            "lastAttemptAt": 0,
            "lastStatusPollAt": 0,
            "lastConvergedAt": 0,
            "lastError": "",
            "lastErrorAt": 0,
        }

    def default_classifier_sync_state(self):
        return {
            "state": "idle",
            "scope": self.classifier_sync_scope,
            "excludedRoots": sorted(CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES),
            "desiredHash": "",
            "actualHash": "",
            "desiredGroupCount": 0,
            "actualGroupCount": 0,
            "originParticipant": "",
            "desiredPublishedAt": 0,
            "lastReceivedAt": 0,
            "lastPublishedAt": 0,
            "lastAppliedAt": 0,
            "lastConvergedAt": 0,
            "lastError": "",
            "lastErrorAt": 0,
        }

    def refresh_classifier_summary_locked(self):
        state = dict(self.classifier_sync_state)
        ready = not self.classifier_sync_enabled
        if self.classifier_sync_enabled:
            desired_hash = (state.get("desiredHash") or "").strip()
            actual_hash = (state.get("actualHash") or "").strip()
            phase = (state.get("state") or "idle").strip() or "idle"
            ready = bool(actual_hash) and phase not in {"pending", "in-progress", "failed"}
            if desired_hash and actual_hash != desired_hash:
                ready = False
            if self.classifier_sync_runtime_error:
                ready = False

        last_error = self.classifier_sync_runtime_error or (state.get("lastError") or "").strip()
        self.status.update(
            {
                "classifierSyncReady": ready,
                "classifierSyncBackend": (
                    self.classifier_sync_backend if self.classifier_sync_enabled else ""
                ),
                "classifierSyncServiceHost": (
                    self.classifier_sync_service_host if self.classifier_sync_enabled else ""
                ),
                "classifierSyncTargetRoles": list(self.classifier_sync_target_roles),
                "classifierSyncScope": state.get("scope", ""),
                "classifierSyncExcludedRoots": list(
                    state.get("excludedRoots") or sorted(CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES)
                ),
                "classifierSyncState": state.get("state", "idle"),
                "classifierSyncDesiredHash": state.get("desiredHash", ""),
                "classifierSyncActualHash": state.get("actualHash", ""),
                "classifierSyncDesiredGroupCount": int(state.get("desiredGroupCount") or 0),
                "classifierSyncActualGroupCount": int(state.get("actualGroupCount") or 0),
                "classifierSyncLastPublishedAt": int(state.get("lastPublishedAt") or 0),
                "classifierSyncLastAppliedAt": int(state.get("lastAppliedAt") or 0),
                "classifierSyncLastConvergedAt": int(state.get("lastConvergedAt") or 0),
                "classifierSyncLastError": last_error,
            }
        )

    def refresh_classifier_summary(self):
        with self.lock:
            self.refresh_classifier_summary_locked()

    def persist_classifier_sync_state(self):
        if not self.classifier_sync_enabled:
            return
        with self.lock:
            payload = {
                "apiVersion": "pe-k8s.puppet.com/v1alpha1",
                "kind": "ConductorRelayClassifierSyncState",
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "state": dict(self.classifier_sync_state),
            }
        write_text_file(self.classifier_sync_state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def load_classifier_sync_state(self):
        state = self.default_classifier_sync_state()
        if self.classifier_sync_enabled and os.path.isfile(self.classifier_sync_state_path):
            try:
                payload = read_json_file(self.classifier_sync_state_path)
                raw_state = payload.get("state") or {}
                if isinstance(raw_state, dict):
                    state.update(raw_state)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                log(f"Failed to load classifier sync state: {error}")
        with self.lock:
            self.classifier_sync_state = state
            self.refresh_classifier_summary_locked()

    def classifier_sync_state_snapshot(self):
        with self.lock:
            return dict(self.classifier_sync_state)

    def merge_classifier_sync_state(self, **updates):
        with self.lock:
            state = dict(self.classifier_sync_state)
            state.update(updates)
            if "lastError" in updates:
                if (updates.get("lastError") or "").strip():
                    state["lastErrorAt"] = int(updates.get("lastErrorAt") or time.time())
                else:
                    state["lastErrorAt"] = 0
                    self.classifier_sync_runtime_error = ""
            self.classifier_sync_state = state
            self.refresh_classifier_summary_locked()
            snapshot = dict(state)
        self.persist_classifier_sync_state()
        return snapshot

    def classifier_sync_uses_cassandra(self):
        return self.classifier_sync_enabled

    @staticmethod
    def classifier_sync_preserved_logical_ids():
        return {
            ALL_NODES_GROUP_ID,
            CLASSIFIER_LOGICAL_ALL_ENVIRONMENTS_ID,
            CLASSIFIER_LOGICAL_PRODUCTION_ENVIRONMENT_ID,
            CLASSIFIER_LOGICAL_DEVELOPMENT_ENVIRONMENT_ID,
            CLASSIFIER_LOGICAL_DEVELOPMENT_ONE_TIME_RUN_EXCEPTION_ID,
            CLASSIFIER_LOGICAL_PE_PATCH_MANAGEMENT_ID,
        }

    def classifier_builtin_logical_id(self, group, parent_logical_id):
        group_id = (group.get("id") or "").strip()
        name = (group.get("name") or "").strip()
        if group_id == ALL_NODES_GROUP_ID and name == "All Nodes":
            return ALL_NODES_GROUP_ID
        if parent_logical_id == ALL_NODES_GROUP_ID:
            if name == "All Environments":
                return CLASSIFIER_LOGICAL_ALL_ENVIRONMENTS_ID
            if name == "PE Patch Management":
                return CLASSIFIER_LOGICAL_PE_PATCH_MANAGEMENT_ID
        if parent_logical_id == CLASSIFIER_LOGICAL_ALL_ENVIRONMENTS_ID:
            if name == "Production environment":
                return CLASSIFIER_LOGICAL_PRODUCTION_ENVIRONMENT_ID
            if name == "Development environment":
                return CLASSIFIER_LOGICAL_DEVELOPMENT_ENVIRONMENT_ID
        if (
            parent_logical_id == CLASSIFIER_LOGICAL_DEVELOPMENT_ENVIRONMENT_ID
            and name == "Development one-time run exception"
        ):
            return CLASSIFIER_LOGICAL_DEVELOPMENT_ONE_TIME_RUN_EXCEPTION_ID
        return ""

    def classifier_group_is_local_exclusion_root(self, group, parent_logical_id):
        if parent_logical_id != ALL_NODES_GROUP_ID:
            return False
        group_id = (group.get("id") or "").strip()
        name = (group.get("name") or "").strip()
        return (
            group_id == LEGACY_CLASSIFIER_ROOT_GROUP_ID
            or name in CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES
        )

    def classifier_project_sync_groups(self, all_groups):
        group_map = {
            (group.get("id") or "").strip(): normalize_classifier_group(group)
            for group in all_groups or []
            if (group.get("id") or "").strip()
        }
        root = group_map.get(ALL_NODES_GROUP_ID)
        if root is None:
            raise RuntimeError("classifier groups are missing the All Nodes root")

        children = {}
        for group in group_map.values():
            children.setdefault((group.get("parent") or "").strip(), []).append(group)
        for entries in children.values():
            entries.sort(key=classifier_group_sort_key)

        projected = []
        logical_to_actual = {}
        queue_items = [(root, ALL_NODES_GROUP_ID)]
        seen_actual_ids = set()
        while queue_items:
            group, parent_logical_id = queue_items.pop(0)
            actual_id = (group.get("id") or "").strip()
            if not actual_id or actual_id in seen_actual_ids:
                continue
            seen_actual_ids.add(actual_id)
            if self.classifier_group_is_local_exclusion_root(group, parent_logical_id):
                continue

            logical_id = self.classifier_builtin_logical_id(group, parent_logical_id) or actual_id
            entry = normalize_classifier_group(group)
            entry["id"] = logical_id
            entry["parent"] = parent_logical_id or (entry.get("parent") or ALL_NODES_GROUP_ID)
            entry["sourceId"] = actual_id
            projected.append(entry)
            logical_to_actual[logical_id] = actual_id

            for child in children.get(actual_id, []):
                queue_items.append((child, logical_id))

        return projected, logical_to_actual

    def retire_legacy_classifier_root(self, all_groups):
        if not self.classifier_sync_enabled:
            return all_groups

        group_map = {
            (group.get("id") or "").strip(): normalize_classifier_group(group)
            for group in all_groups or []
            if (group.get("id") or "").strip()
        }
        children = {}
        for group in group_map.values():
            children.setdefault((group.get("parent") or "").strip(), []).append(group)

        legacy_group = group_map.get(LEGACY_CLASSIFIER_ROOT_GROUP_ID)
        if legacy_group is None:
            for group in group_map.values():
                if (
                    (group.get("name") or "").strip() == LEGACY_CLASSIFIER_ROOT_GROUP_NAME
                    and (group.get("parent") or "").strip() == ALL_NODES_GROUP_ID
                ):
                    legacy_group = group
                    break
        if legacy_group is None:
            return all_groups

        legacy_group_id = (legacy_group.get("id") or "").strip()
        if children.get(legacy_group_id):
            return all_groups

        self.classifier_request(
            "DELETE",
            f"/groups/{legacy_group_id}",
            expected_statuses={204, 404},
        )
        log(
            f"Removed legacy classifier sync root {LEGACY_CLASSIFIER_ROOT_GROUP_NAME} "
            f"({legacy_group_id})"
        )
        return [
            group
            for group in all_groups or []
            if (group.get("id") or "").strip() != legacy_group_id
        ]

    def issue_service_token(self, service_host, username, password, lifetime, label_prefix):
        if not username or not password:
            raise RuntimeError("service credentials are not configured")

        context = self.build_local_service_context()
        payload = {
            "login": username,
            "password": password,
            "lifetime": lifetime,
            "label": f"{label_prefix}-{uuid.uuid4().hex[:12]}",
        }
        status_code, body = http_request_json(
            "POST",
            f"https://{service_host}:4433/rbac-api/v1/auth/token",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            payload=payload,
            context=context,
            timeout=min(self.code_deploy_request_timeout_seconds, 60),
        )
        if status_code != 200:
            raise RuntimeError(f"RBAC token request returned {status_code}: {body}")
        decoded = json.loads(body)
        if isinstance(decoded, str):
            return decoded
        if isinstance(decoded, dict) and (decoded.get("token") or "").strip():
            return decoded["token"].strip()
        raise RuntimeError("RBAC token request returned an unexpected payload")

    def local_console_request(
        self,
        method,
        path,
        payload=None,
        *,
        service_host,
        username,
        password,
        token_lifetime,
        token_label,
        expected_statuses,
        timeout=60,
    ):
        token = self.issue_service_token(
            service_host,
            username,
            password,
            token_lifetime,
            token_label,
        )
        context = self.build_local_service_context()
        status_code, body = http_request_json(
            method,
            f"https://{service_host}:4433{path}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Authentication": token,
            },
            payload=payload,
            context=context,
            timeout=timeout,
        )
        if status_code not in expected_statuses:
            raise RuntimeError(f"Console API {method} {path} returned {status_code}: {body}")
        if not body:
            return status_code, None
        try:
            return status_code, json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Console API {method} {path} returned non-JSON response: {body}") from error

    def classifier_request(self, method, path, payload=None, expected_statuses=None):
        return self.local_console_request(
            method,
            f"/classifier-api/v1{path}",
            payload=payload,
            service_host=self.classifier_sync_service_host,
            username=self.classifier_sync_username,
            password=self.classifier_sync_password,
            token_lifetime=self.classifier_sync_token_lifetime,
            token_label=self.classifier_sync_token_label,
            expected_statuses=expected_statuses or {200},
            timeout=self.code_deploy_request_timeout_seconds,
        )

    def fetch_classifier_groups(self):
        status_code, payload = self.classifier_request("GET", "/groups", expected_statuses={200})
        if status_code != 200 or not isinstance(payload, list):
            raise RuntimeError(f"classifier groups request returned {status_code}: {payload}")
        groups = []
        for item in payload:
            normalized = normalize_classifier_group(item)
            if normalized["id"]:
                groups.append(normalized)
        return groups

    def read_local_classifier_state(self):
        all_groups = self.fetch_classifier_groups()
        all_groups = self.retire_legacy_classifier_root(all_groups)
        groups, _ = self.classifier_project_sync_groups(all_groups)
        payload = {
            "scope": self.classifier_sync_scope,
            "excludedRoots": sorted(CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES),
            "groups": groups,
            "groupCount": len(groups),
        }
        payload["hash"] = classifier_state_hash(groups)
        return payload

    def refresh_local_classifier_sync_state(self):
        if not self.classifier_sync_enabled:
            return

        observed_at = int(time.time())
        local_state = self.read_local_classifier_state()
        snapshot = self.classifier_sync_state_snapshot()
        desired_hash = (snapshot.get("desiredHash") or "").strip()
        actual_hash = (local_state.get("hash") or "").strip()
        phase = (snapshot.get("state") or "idle").strip() or "idle"
        origin_participant = (snapshot.get("originParticipant") or "").strip()

        updates = {
            "scope": self.classifier_sync_scope,
            "excludedRoots": sorted(CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES),
            "actualHash": actual_hash,
            "actualGroupCount": int(local_state.get("groupCount") or 0),
        }
        if desired_hash and desired_hash == actual_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        elif desired_hash and origin_participant and origin_participant != self.pod_name and phase in {
            "pending",
            "in-progress",
            "failed",
        }:
            updates["state"] = phase
        else:
            updates.update(
                {
                    "state": "observed",
                    "desiredHash": actual_hash,
                    "desiredGroupCount": int(local_state.get("groupCount") or 0),
                    "originParticipant": self.pod_name,
                    "desiredPublishedAt": observed_at,
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        self.classifier_sync_runtime_error = ""
        self.merge_classifier_sync_state(**updates)

    def build_classifier_state_payload(self, state, published_at):
        state_payload = {
            "scope": state.get("scope", self.classifier_sync_scope),
            "excludedRoots": list(
                state.get("excludedRoots") or sorted(CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES)
            ),
            "hash": state.get("hash", ""),
            "groupCount": int(state.get("groupCount") or 0),
        }
        return {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorRelayClassifierState",
            "publishedAt": published_at,
            "origin": {
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "role": self.relay_role,
                "segment": self.segment_name,
            },
            "targetRoles": list(self.classifier_sync_target_roles),
            "state": state_payload,
        }

    def ensure_classifier_sync_cassandra_session(self):
        if not self.classifier_sync_enabled:
            raise RuntimeError("classifier sync is not enabled")
        if not self.classifier_sync_cassandra_contact_points:
            raise RuntimeError("no Cassandra contact points configured for classifier sync")
        if Cluster is None or ConsistencyLevel is None or SimpleStatement is None:
            raise RuntimeError("cassandra-driver is not available in the Conductor image")

        with self.lock:
            if self.classifier_sync_cassandra_session is not None:
                return self.classifier_sync_cassandra_session

            cluster = Cluster(
                contact_points=self.classifier_sync_cassandra_contact_points,
                port=self.classifier_sync_cassandra_port,
            )
            session = cluster.connect()
            keyspace = self.classifier_sync_cassandra_keyspace
            table = self.classifier_sync_cassandra_table
            replication_factor = max(1, self.classifier_sync_cassandra_replication_factor)
            session.execute(
                f"""
                create keyspace if not exists {keyspace}
                with replication = {{
                    'class': 'SimpleStrategy',
                    'replication_factor': {replication_factor}
                }}
                """
            )
            session.set_keyspace(keyspace)
            session.execute(
                f"""
                create table if not exists {table} (
                    scope text primary key,
                    published_at bigint,
                    origin_participant text,
                    state_hash text,
                    payload text
                )
                """
            )
            self.classifier_sync_cassandra_cluster = cluster
            self.classifier_sync_cassandra_session = session
            return session

    def read_shared_classifier_state(self):
        session = self.ensure_classifier_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"select scope, published_at, origin_participant, state_hash, payload "
                f"from {self.classifier_sync_cassandra_table} where scope = %s"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        row = session.execute(statement, (self.classifier_sync_scope,)).one()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload or "{}")
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"invalid shared classifier sync payload: {error}") from error
        payload_hash = ((payload or {}).get("hash") or "").strip()
        expected_hash = (row.state_hash or "").strip()
        if expected_hash and payload_hash and payload_hash != expected_hash:
            raise RuntimeError(
                "shared classifier sync payload hash mismatch: "
                f"expected {expected_hash}, got {payload_hash}"
            )
        return {
            "scope": (row.scope or "").strip(),
            "publishedAt": int(row.published_at or 0),
            "originParticipant": (row.origin_participant or "").strip(),
            "stateHash": expected_hash or payload_hash,
            "state": payload if isinstance(payload, dict) else {},
        }

    def wait_for_shared_classifier_state(self, received_published_at=0, timeout_seconds=15):
        deadline = time.time() + max(1, int(timeout_seconds))
        last_state = None
        last_published_at = 0
        while time.time() < deadline:
            shared = self.read_shared_classifier_state()
            if shared is not None:
                shared_published_at = int(shared.get("publishedAt") or 0)
                if not received_published_at or not shared_published_at or shared_published_at >= received_published_at:
                    return shared
                last_state = shared
                last_published_at = shared_published_at
            time.sleep(1)
        if last_state is not None and received_published_at:
            raise RuntimeError(
                "shared classifier sync state is older than the received intent: "
                f"{last_published_at} < {received_published_at}"
            )
        return last_state

    def upsert_shared_classifier_state(self, state, published_at):
        state_hash = ((state or {}).get("hash") or "").strip()
        if not state_hash:
            raise RuntimeError("classifier sync state is missing a hash")
        session = self.ensure_classifier_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"insert into {self.classifier_sync_cassandra_table} "
                "(scope, published_at, origin_participant, state_hash, payload) "
                "values (%s, %s, %s, %s, %s)"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        session.execute(
            statement,
            (
                self.classifier_sync_scope,
                int(published_at or time.time()),
                self.pod_name,
                state_hash,
                stable_json(state),
            ),
        )

    def publish_classifier_state(self):
        if (
            not self.classifier_sync_enabled
            or self.connection is None
            or self.channel is None
            or self.bundle is None
        ):
            return

        state = self.read_local_classifier_state()
        now = int(time.time())
        state_hash = (state.get("hash") or "").strip()
        should_publish = state_hash != self.last_published_classifier_hash or now >= self.next_classifier_publish
        if not should_publish:
            return

        snapshot = self.classifier_sync_state_snapshot()
        version_at = max(int(snapshot.get("desiredPublishedAt") or 0), now)
        if (snapshot.get("desiredHash") or "").strip() != state_hash:
            version_at = now

        self.upsert_shared_classifier_state(state, version_at)

        payload = self.build_classifier_state_payload(state, version_at)
        self.channel.basic_publish(
            exchange=self.bundle["hub"]["exchanges"]["data"],
            routing_key=f"relay.classifier-state.{sanitize_fragment(self.pod_name)}",
            body=stable_json(payload).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )
        self.last_published_classifier_hash = state_hash
        self.next_classifier_publish = now + self.publish_interval
        self.merge_classifier_sync_state(
            state="converged",
            scope=self.classifier_sync_scope,
            excludedRoots=sorted(CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES),
            desiredHash=state_hash,
            actualHash=state_hash,
            desiredGroupCount=int(state.get("groupCount") or 0),
            actualGroupCount=int(state.get("groupCount") or 0),
            originParticipant=self.pod_name,
            desiredPublishedAt=version_at,
            lastPublishedAt=now,
            lastConvergedAt=now,
            lastError="",
        )

    def reconcile_classifier_state(self, state_payload, published_at, origin_participant):
        shared = self.wait_for_shared_classifier_state(published_at)
        if shared is None:
            raise RuntimeError("shared classifier sync state is missing from Cassandra")
        state_payload = dict(shared.get("state") or {})
        origin_participant = (shared.get("originParticipant") or "").strip() or origin_participant
        published_at = int(shared.get("publishedAt") or 0) or int(published_at or time.time())

        desired_groups = []
        for raw_group in (state_payload.get("groups") or []):
            group = normalize_classifier_group(raw_group)
            source_id = ((raw_group or {}).get("sourceId") or "").strip()
            if source_id:
                group["sourceId"] = source_id
            desired_groups.append(group)
        if not desired_groups:
            raise RuntimeError("classifier sync payload does not contain any managed groups")

        desired_map = {
            (group.get("id") or "").strip(): group
            for group in desired_groups
            if (group.get("id") or "").strip()
        }
        if ALL_NODES_GROUP_ID not in desired_map:
            raise RuntimeError("classifier sync payload is missing the All Nodes root")

        desired_depth_cache = {}
        desired_groups.sort(
            key=lambda group: (
                classifier_group_depth(
                    desired_map,
                    group.get("id"),
                    ALL_NODES_GROUP_ID,
                    desired_depth_cache,
                ),
                classifier_group_sort_key(group),
            )
        )

        current_groups = self.fetch_classifier_groups()
        current_groups = self.retire_legacy_classifier_root(current_groups)
        current_projected, current_logical_to_actual = self.classifier_project_sync_groups(current_groups)
        current_map = {
            (group.get("id") or "").strip(): group
            for group in current_projected
            if (group.get("id") or "").strip()
        }

        for group in desired_groups:
            logical_id = (group.get("id") or "").strip()
            if not logical_id or logical_id == ALL_NODES_GROUP_ID:
                continue
            parent_logical_id = (group.get("parent") or "").strip() or ALL_NODES_GROUP_ID
            target_group_id = (
                current_logical_to_actual.get(logical_id)
                or ((group.get("sourceId") or "").strip())
                or logical_id
            )
            target_parent_id = current_logical_to_actual.get(parent_logical_id) or parent_logical_id
            materialized_group = normalize_classifier_group(group)
            materialized_group["parent"] = target_parent_id
            self.classifier_request(
                "PUT",
                f"/groups/{target_group_id}",
                payload=classifier_group_put_payload(materialized_group),
                expected_statuses={200, 201},
            )
            current_logical_to_actual[logical_id] = target_group_id

        extra_groups = [
            group
            for group_id, group in current_map.items()
            if group_id not in desired_map
            and group_id not in self.classifier_sync_preserved_logical_ids()
        ]
        current_depth_cache = {}
        extra_groups.sort(
            key=lambda group: (
                -classifier_group_depth(
                    current_map,
                    group.get("id"),
                    ALL_NODES_GROUP_ID,
                    current_depth_cache,
                ),
                classifier_group_sort_key(group),
            )
        )
        for group in extra_groups:
            logical_id = (group.get("id") or "").strip()
            target_group_id = (
                current_logical_to_actual.get(logical_id)
                or ((group.get("sourceId") or "").strip())
                or logical_id
            )
            self.classifier_request(
                "DELETE",
                f"/groups/{target_group_id}",
                expected_statuses={204, 404},
            )

        local_state = self.read_local_classifier_state()
        actual_hash = (local_state.get("hash") or "").strip()
        desired_hash = (state_payload.get("hash") or "").strip()
        updates = {
            "scope": state_payload.get("scope") or self.classifier_sync_scope,
            "excludedRoots": list(
                state_payload.get("excludedRoots") or sorted(CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES)
            ),
            "desiredHash": desired_hash,
            "actualHash": actual_hash,
            "desiredGroupCount": len(desired_groups),
            "actualGroupCount": int(local_state.get("groupCount") or 0),
            "originParticipant": origin_participant,
            "desiredPublishedAt": int(published_at or time.time()),
            "lastAppliedAt": int(time.time()),
        }
        if actual_hash == desired_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": int(time.time()),
                    "lastError": "",
                }
            )
        else:
            updates.update(
                {
                    "state": "failed",
                    "lastError": (
                        "classifier managed-domain hash mismatch after apply: "
                        f"expected {desired_hash}, got {actual_hash or 'none'}"
                    ),
                }
            )
        self.classifier_sync_runtime_error = ""
        self.merge_classifier_sync_state(**updates)

    def handle_remote_classifier_state(self, payload):
        if not self.classifier_sync_enabled:
            return

        origin = payload.get("origin") or {}
        origin_participant = (origin.get("participant") or "").strip()
        if not origin_participant or origin_participant == self.pod_name:
            return

        target_roles = payload.get("targetRoles") or []
        if target_roles and self.relay_role not in target_roles:
            return

        state_payload = payload.get("state") or {}
        desired_hash = (state_payload.get("hash") or "").strip()
        if not desired_hash:
            raise RuntimeError("classifier sync payload is missing a hash")

        published_at = int(payload.get("publishedAt") or 0)
        shared_state = self.wait_for_shared_classifier_state(published_at)
        if shared_state is None:
            raise RuntimeError("shared classifier sync state is missing from Cassandra")
        shared_hash = (shared_state.get("stateHash") or "").strip()
        if shared_hash:
            desired_hash = shared_hash
        published_at = int(shared_state.get("publishedAt") or 0) or published_at
        origin_participant = (
            (shared_state.get("originParticipant") or "").strip() or origin_participant
        )
        shared_payload = shared_state.get("state") or {}
        if isinstance(shared_payload, dict):
            state_payload = shared_payload

        current_state = self.classifier_sync_state_snapshot()
        current_desired_hash = (current_state.get("desiredHash") or "").strip()
        current_actual_hash = (current_state.get("actualHash") or "").strip()
        current_published_at = int(current_state.get("desiredPublishedAt") or 0)
        if current_published_at and published_at and published_at < current_published_at:
            return
        if (
            current_published_at
            and published_at
            and published_at == current_published_at
            and desired_hash == current_desired_hash
        ):
            return
        if desired_hash == current_actual_hash and desired_hash == current_desired_hash:
            self.merge_classifier_sync_state(
                state="converged",
                lastReceivedAt=int(time.time()),
                lastConvergedAt=int(time.time()),
                lastError="",
            )
            return

        self.merge_classifier_sync_state(
            state="pending",
            scope=state_payload.get("scope") or self.classifier_sync_scope,
            excludedRoots=list(
                state_payload.get("excludedRoots") or sorted(CLASSIFIER_LOCAL_EXCLUDE_ROOT_NAMES)
            ),
            desiredHash=desired_hash,
            desiredGroupCount=int(state_payload.get("groupCount") or len(state_payload.get("groups") or [])),
            originParticipant=origin_participant,
            desiredPublishedAt=published_at or int(time.time()),
            lastReceivedAt=int(time.time()),
            lastError="",
        )
        log(
            "Received classifier sync intent at "
            f"{desired_hash[:12]} from {origin_participant}"
        )
        self.reconcile_classifier_state(state_payload, published_at, origin_participant)

    def default_rbac_sync_state(self):
        return {
            "state": "idle",
            "scope": self.rbac_sync_scope,
            "excludedTokenLabelPrefixes": list(self.rbac_sync_excluded_token_label_prefixes),
            "desiredHash": "",
            "actualHash": "",
            "desiredTableCount": 0,
            "actualTableCount": 0,
            "desiredRowCount": 0,
            "actualRowCount": 0,
            "authFileCount": 0,
            "originParticipant": "",
            "desiredPublishedAt": 0,
            "lastReceivedAt": 0,
            "lastPublishedAt": 0,
            "lastAppliedAt": 0,
            "lastConvergedAt": 0,
            "lastError": "",
            "lastErrorAt": 0,
        }

    def refresh_rbac_summary_locked(self):
        state = dict(self.rbac_sync_state)
        ready = not self.rbac_sync_enabled
        if self.rbac_sync_enabled:
            desired_hash = (state.get("desiredHash") or "").strip()
            actual_hash = (state.get("actualHash") or "").strip()
            phase = (state.get("state") or "idle").strip() or "idle"
            ready = bool(actual_hash) and phase not in {"pending", "in-progress", "failed"}
            if desired_hash and actual_hash != desired_hash:
                ready = False
            if self.rbac_sync_runtime_error:
                ready = False

        last_error = self.rbac_sync_runtime_error or (state.get("lastError") or "").strip()
        self.status.update(
            {
                "rbacSyncReady": ready,
                "rbacSyncBackend": self.rbac_sync_backend,
                "rbacSyncTargetRoles": list(self.rbac_sync_target_roles),
                "rbacSyncScope": state.get("scope", ""),
                "rbacSyncExcludedTokenLabelPrefixes": list(
                    state.get("excludedTokenLabelPrefixes") or self.rbac_sync_excluded_token_label_prefixes
                ),
                "rbacSyncState": state.get("state", "idle"),
                "rbacSyncDesiredHash": state.get("desiredHash", ""),
                "rbacSyncActualHash": state.get("actualHash", ""),
                "rbacSyncDesiredTableCount": int(state.get("desiredTableCount") or 0),
                "rbacSyncActualTableCount": int(state.get("actualTableCount") or 0),
                "rbacSyncDesiredRowCount": int(state.get("desiredRowCount") or 0),
                "rbacSyncActualRowCount": int(state.get("actualRowCount") or 0),
                "rbacSyncAuthFileCount": int(state.get("authFileCount") or 0),
                "rbacSyncLastPublishedAt": int(state.get("lastPublishedAt") or 0),
                "rbacSyncLastAppliedAt": int(state.get("lastAppliedAt") or 0),
                "rbacSyncLastConvergedAt": int(state.get("lastConvergedAt") or 0),
                "rbacSyncLastError": last_error,
            }
        )

    def refresh_rbac_summary(self):
        with self.lock:
            self.refresh_rbac_summary_locked()

    def default_rbac_token_sync_state(self):
        return {
            "state": "idle",
            "scope": self.rbac_token_sync_scope,
            "excludedTokenLabelPrefixes": list(self.rbac_sync_excluded_token_label_prefixes),
            "desiredHash": "",
            "actualHash": "",
            "desiredTableCount": 0,
            "actualTableCount": 0,
            "desiredRowCount": 0,
            "actualRowCount": 0,
            "authFileCount": 0,
            "originParticipant": "",
            "desiredPublishedAt": 0,
            "lastReceivedAt": 0,
            "lastPublishedAt": 0,
            "lastAppliedAt": 0,
            "lastConvergedAt": 0,
            "lastError": "",
            "lastErrorAt": 0,
        }

    def refresh_rbac_token_summary_locked(self):
        state = dict(self.rbac_token_sync_state)
        ready = not self.rbac_token_sync_enabled
        if self.rbac_token_sync_enabled:
            desired_hash = (state.get("desiredHash") or "").strip()
            actual_hash = (state.get("actualHash") or "").strip()
            phase = (state.get("state") or "idle").strip() or "idle"
            ready = bool(actual_hash) and phase not in {"pending", "in-progress", "failed"}
            if desired_hash and actual_hash != desired_hash:
                ready = False
            if self.rbac_token_sync_runtime_error:
                ready = False

        last_error = self.rbac_token_sync_runtime_error or (state.get("lastError") or "").strip()
        self.status.update(
            {
                "rbacTokenSyncReady": ready,
                "rbacTokenSyncTargetRoles": list(self.rbac_token_sync_target_roles),
                "rbacTokenSyncScope": state.get("scope", ""),
                "rbacTokenSyncExcludedTokenLabelPrefixes": list(
                    state.get("excludedTokenLabelPrefixes") or self.rbac_sync_excluded_token_label_prefixes
                ),
                "rbacTokenSyncState": state.get("state", "idle"),
                "rbacTokenSyncDesiredHash": state.get("desiredHash", ""),
                "rbacTokenSyncActualHash": state.get("actualHash", ""),
                "rbacTokenSyncDesiredTableCount": int(state.get("desiredTableCount") or 0),
                "rbacTokenSyncActualTableCount": int(state.get("actualTableCount") or 0),
                "rbacTokenSyncDesiredRowCount": int(state.get("desiredRowCount") or 0),
                "rbacTokenSyncActualRowCount": int(state.get("actualRowCount") or 0),
                "rbacTokenSyncAuthFileCount": int(state.get("authFileCount") or 0),
                "rbacTokenSyncLastPublishedAt": int(state.get("lastPublishedAt") or 0),
                "rbacTokenSyncLastAppliedAt": int(state.get("lastAppliedAt") or 0),
                "rbacTokenSyncLastConvergedAt": int(state.get("lastConvergedAt") or 0),
                "rbacTokenSyncLastError": last_error,
            }
        )

    def refresh_rbac_token_summary(self):
        with self.lock:
            self.refresh_rbac_token_summary_locked()

    def persist_rbac_sync_state(self):
        if not self.rbac_sync_enabled:
            return
        with self.lock:
            payload = {
                "apiVersion": "pe-k8s.puppet.com/v1alpha1",
                "kind": "ConductorRelayRbacSyncState",
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "state": dict(self.rbac_sync_state),
            }
        write_text_file(self.rbac_sync_state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def load_rbac_sync_state(self):
        state = self.default_rbac_sync_state()
        if self.rbac_sync_enabled and os.path.isfile(self.rbac_sync_state_path):
            try:
                payload = read_json_file(self.rbac_sync_state_path)
                raw_state = payload.get("state") or {}
                if isinstance(raw_state, dict):
                    state.update(raw_state)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                log(f"Failed to load RBAC sync state: {error}")
        with self.lock:
            self.rbac_sync_state = state
            self.refresh_rbac_summary_locked()

    def persist_rbac_token_sync_state(self):
        if not self.rbac_token_sync_enabled:
            return
        with self.lock:
            payload = {
                "apiVersion": "pe-k8s.puppet.com/v1alpha1",
                "kind": "ConductorRelayRbacTokenSyncState",
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "state": dict(self.rbac_token_sync_state),
            }
        write_text_file(
            self.rbac_token_sync_state_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def load_rbac_token_sync_state(self):
        state = self.default_rbac_token_sync_state()
        if self.rbac_token_sync_enabled and os.path.isfile(self.rbac_token_sync_state_path):
            try:
                payload = read_json_file(self.rbac_token_sync_state_path)
                raw_state = payload.get("state") or {}
                if isinstance(raw_state, dict):
                    state.update(raw_state)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                log(f"Failed to load RBAC token sync state: {error}")
        with self.lock:
            self.rbac_token_sync_state = state
            self.refresh_rbac_token_summary_locked()

    def rbac_sync_state_snapshot(self):
        with self.lock:
            return dict(self.rbac_sync_state)

    def merge_rbac_sync_state(self, **updates):
        with self.lock:
            state = dict(self.rbac_sync_state)
            state.update(updates)
            if "lastError" in updates:
                if (updates.get("lastError") or "").strip():
                    state["lastErrorAt"] = int(updates.get("lastErrorAt") or time.time())
                else:
                    state["lastErrorAt"] = 0
            self.rbac_sync_state = state
            self.refresh_rbac_summary_locked()
            snapshot = dict(state)
        self.persist_rbac_sync_state()
        return snapshot

    def rbac_token_sync_state_snapshot(self):
        with self.lock:
            return dict(self.rbac_token_sync_state)

    def merge_rbac_token_sync_state(self, **updates):
        with self.lock:
            state = dict(self.rbac_token_sync_state)
            state.update(updates)
            if "lastError" in updates:
                if (updates.get("lastError") or "").strip():
                    state["lastErrorAt"] = int(updates.get("lastErrorAt") or time.time())
                else:
                    state["lastErrorAt"] = 0
                    self.rbac_token_sync_runtime_error = ""
            self.rbac_token_sync_state = state
            self.refresh_rbac_token_summary_locked()
            snapshot = dict(state)
        self.persist_rbac_token_sync_state()
        return snapshot

    def rbac_sync_table_names(self):
        return [
            table_name
            for table_name in RBAC_SYNC_TABLE_NAMES
            if not (self.rbac_token_sync_enabled and table_name == "tokens")
        ]

    def rbac_sync_auth_file_names(self):
        if self.rbac_token_sync_enabled:
            return []
        return list(RBAC_SYNC_REQUIRED_AUTH_FILES) + list(RBAC_SYNC_OPTIONAL_AUTH_FILES)

    def rbac_token_sync_auth_file_names(self):
        return list(RBAC_SYNC_REQUIRED_AUTH_FILES) + list(RBAC_SYNC_OPTIONAL_AUTH_FILES)

    def rbac_sync_uses_cassandra(self):
        return self.rbac_sync_enabled

    def rbac_sync_conf_text(self):
        if not os.path.isfile(self.rbac_sync_rbac_conf_path):
            raise RuntimeError(f"RBAC config not found: {self.rbac_sync_rbac_conf_path}")
        with open(self.rbac_sync_rbac_conf_path, "r", encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def rbac_sync_hocon_string(content, key):
        match = re.search(rf'^\s*{re.escape(key)}\s*:\s*"([^"]+)"', content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    def rbac_sync_auth_file_paths(self):
        conf_text = self.rbac_sync_conf_text()
        current = {
            "keysJson": self.rbac_sync_keys_path,
            "tokenPrivateKey": self.rbac_sync_hocon_string(conf_text, "token-private-key"),
            "tokenPublicKey": self.rbac_sync_hocon_string(conf_text, "token-public-key"),
            "samlKey": self.rbac_sync_hocon_string(conf_text, "saml-key"),
            "samlCert": self.rbac_sync_hocon_string(conf_text, "saml-cert"),
        }
        target = {
            "keysJson": self.rbac_sync_keys_path,
            "tokenPrivateKey": self.rbac_sync_shared_token_private_key_path,
            "tokenPublicKey": self.rbac_sync_shared_token_public_key_path,
            "samlKey": self.rbac_sync_shared_saml_key_path,
            "samlCert": self.rbac_sync_shared_saml_cert_path,
        }
        return current, target

    def rbac_sync_db_config(self):
        if not os.path.isfile(self.rbac_sync_rbac_database_conf_path):
            raise RuntimeError(
                f"RBAC database config not found: {self.rbac_sync_rbac_database_conf_path}"
            )
        with open(self.rbac_sync_rbac_database_conf_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        subname = self.rbac_sync_hocon_string(content, "subname")
        user = self.rbac_sync_hocon_string(content, "user")
        if not subname or not user:
            raise RuntimeError("RBAC database configuration is incomplete")
        parsed = urllib.parse.urlsplit(f"postgresql:{subname}")
        params = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items() if values}
        database = parsed.path.lstrip("/")
        sslkey = params.get("sslkey", "")
        if sslkey.endswith(".pk8"):
            pem_key = re.sub(r"\.pk8$", ".pem", sslkey)
            if os.path.isfile(pem_key):
                sslkey = pem_key
        return {
            "host": parsed.hostname or "",
            "port": int(parsed.port or 5432),
            "database": database,
            "user": user,
            "sslrootcert": params.get("sslrootcert", ""),
            "sslkey": sslkey,
            "sslcert": params.get("sslcert", ""),
        }

    def rbac_db_connection(self):
        config = self.rbac_sync_db_config()
        context = ssl.create_default_context(cafile=config["sslrootcert"])
        context.check_hostname = True
        context.load_cert_chain(
            certfile=config["sslcert"],
            keyfile=config["sslkey"],
        )
        return pg8000.dbapi.connect(
            user=config["user"],
            host=config["host"],
            port=config["port"],
            database=config["database"],
            ssl_context=context,
            timeout=15,
        )

    @staticmethod
    def normalize_rbac_rows_for_hash(table_rows):
        normalized_tables = {}
        for table_name in RBAC_SYNC_TABLE_NAMES:
            volatile_fields = RBAC_SYNC_VOLATILE_FIELDS.get(table_name, set())
            normalized_rows = []
            for row in list(table_rows.get(table_name) or []):
                if not volatile_fields:
                    normalized_rows.append(row)
                    continue
                normalized_row = dict(row)
                for field_name in volatile_fields:
                    if field_name in normalized_row:
                        normalized_row[field_name] = None
                normalized_rows.append(normalized_row)
            normalized_tables[table_name] = normalized_rows
        return normalized_tables

    @staticmethod
    def rbac_rows_hash(table_rows, auth_files, excluded_token_label_prefixes):
        payload = {
            "tables": RelayRuntime.normalize_rbac_rows_for_hash(table_rows),
            "authFiles": dict(sorted((auth_files or {}).items())),
            "excludedTokenLabelPrefixes": list(excluded_token_label_prefixes or []),
        }
        return sha256_text(stable_json(payload))

    def rbac_token_is_excluded(self, row):
        label = ((row or {}).get("label") or "").strip()
        return any(label.startswith(prefix) for prefix in self.rbac_sync_excluded_token_label_prefixes)

    def rbac_query_rows(self, cursor, query):
        wrapped = f"select coalesce(json_agg(row_to_json(t)), '[]'::json)::text from ({query}) t"
        cursor.execute(wrapped)
        value = cursor.fetchone()[0]
        if not value:
            return []
        return json.loads(value)

    def read_local_rbac_state(self):
        current_paths, target_paths = self.rbac_sync_auth_file_paths()
        auth_files = {}
        allowed_auth_file_names = set(self.rbac_sync_auth_file_names())
        for logical_name, source_path in {**RBAC_SYNC_REQUIRED_AUTH_FILES, **RBAC_SYNC_OPTIONAL_AUTH_FILES}.items():
            if logical_name not in allowed_auth_file_names:
                continue
            actual_path = target_paths.get(logical_name)
            if logical_name != "keysJson" and actual_path and os.path.isfile(actual_path):
                source_path = actual_path
            else:
                source_path = current_paths.get(logical_name) or source_path
            if not source_path or not os.path.isfile(source_path):
                if logical_name in RBAC_SYNC_OPTIONAL_AUTH_FILES:
                    continue
                raise RuntimeError(f"RBAC auth file is missing: {source_path or logical_name}")
            with open(source_path, "rb") as handle:
                auth_files[logical_name] = base64.b64encode(handle.read()).decode("ascii")

        table_rows = {}
        connection = self.rbac_db_connection()
        try:
            cursor = connection.cursor()
            repaired = self.ensure_local_rbac_protected_rows(cursor)
            if repaired:
                connection.commit()
            table_names = self.rbac_sync_table_names()
            for table_name in table_names:
                rows = self.rbac_query_rows(cursor, RBAC_SYNC_SELECT_QUERIES[table_name])
                if table_name == "tokens":
                    rows = [row for row in rows if not self.rbac_token_is_excluded(row)]
                table_rows[table_name] = rows
            cursor.close()
        finally:
            connection.close()

        row_count = sum(len(rows) for rows in table_rows.values())
        payload = {
            "scope": self.rbac_sync_scope,
            "excludedTokenLabelPrefixes": list(self.rbac_sync_excluded_token_label_prefixes),
            "tableNames": list(table_names),
            "tables": table_rows,
            "tableCount": len(table_names),
            "rowCount": row_count,
            "authFiles": auth_files,
            "authFileCount": len(auth_files),
        }
        payload["hash"] = self.rbac_rows_hash(
            table_rows,
            auth_files,
            self.rbac_sync_excluded_token_label_prefixes,
        )
        return payload

    def refresh_local_rbac_sync_state(self):
        if not self.rbac_sync_enabled:
            return

        observed_at = int(time.time())
        local_state = self.read_local_rbac_state()
        snapshot = self.rbac_sync_state_snapshot()
        desired_hash = (snapshot.get("desiredHash") or "").strip()
        actual_hash = (local_state.get("hash") or "").strip()
        phase = (snapshot.get("state") or "idle").strip() or "idle"
        origin_participant = (snapshot.get("originParticipant") or "").strip()

        updates = {
            "scope": self.rbac_sync_scope,
            "excludedTokenLabelPrefixes": list(self.rbac_sync_excluded_token_label_prefixes),
            "actualHash": actual_hash,
            "actualTableCount": int(local_state.get("tableCount") or 0),
            "actualRowCount": int(local_state.get("rowCount") or 0),
            "authFileCount": int(local_state.get("authFileCount") or 0),
        }
        if desired_hash and desired_hash == actual_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        elif desired_hash and origin_participant and origin_participant != self.pod_name and phase in {
            "pending",
            "in-progress",
            "failed",
        }:
            updates["state"] = phase
        else:
            updates.update(
                {
                    "state": "observed",
                    "desiredHash": actual_hash,
                    "desiredTableCount": int(local_state.get("tableCount") or 0),
                    "desiredRowCount": int(local_state.get("rowCount") or 0),
                    "originParticipant": self.pod_name,
                    "desiredPublishedAt": observed_at,
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        self.rbac_sync_runtime_error = ""
        self.merge_rbac_sync_state(**updates)

    def build_rbac_state_payload(self, state, published_at):
        state_payload = {
            "scope": state.get("scope", self.rbac_sync_scope),
            "excludedTokenLabelPrefixes": list(self.rbac_sync_excluded_token_label_prefixes),
            "hash": state.get("hash", ""),
            "tableCount": int(state.get("tableCount") or 0),
            "rowCount": int(state.get("rowCount") or 0),
            "authFileCount": int(state.get("authFileCount") or 0),
        }
        return {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorRelayRbacState",
            "publishedAt": published_at,
            "origin": {
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "role": self.relay_role,
                "segment": self.segment_name,
            },
            "targetRoles": list(self.rbac_sync_target_roles),
            "state": state_payload,
        }

    def record_published_rbac_state(self, state, published_at, now):
        state_hash = (state.get("hash") or "").strip()
        self.merge_rbac_sync_state(
            state="converged",
            scope=self.rbac_sync_scope,
            excludedTokenLabelPrefixes=list(self.rbac_sync_excluded_token_label_prefixes),
            desiredHash=state_hash,
            actualHash=state_hash,
            desiredTableCount=int(state.get("tableCount") or 0),
            actualTableCount=int(state.get("tableCount") or 0),
            desiredRowCount=int(state.get("rowCount") or 0),
            actualRowCount=int(state.get("rowCount") or 0),
            authFileCount=int(state.get("authFileCount") or 0),
            originParticipant=self.pod_name,
            desiredPublishedAt=published_at,
            lastPublishedAt=now,
            lastConvergedAt=now,
            lastError="",
        )

    def ensure_rbac_sync_cassandra_session(self):
        if not self.rbac_sync_enabled:
            raise RuntimeError("RBAC sync is not enabled")
        if not self.rbac_sync_cassandra_contact_points:
            raise RuntimeError("no Cassandra contact points configured for RBAC sync")
        if Cluster is None or ConsistencyLevel is None or SimpleStatement is None:
            raise RuntimeError("cassandra-driver is not available in the Conductor image")

        with self.lock:
            if self.rbac_sync_cassandra_session is not None:
                return self.rbac_sync_cassandra_session

            cluster = Cluster(
                contact_points=self.rbac_sync_cassandra_contact_points,
                port=self.rbac_sync_cassandra_port,
            )
            session = cluster.connect()
            keyspace = self.rbac_sync_cassandra_keyspace
            table = self.rbac_sync_cassandra_table
            replication_factor = max(1, self.rbac_sync_cassandra_replication_factor)
            session.execute(
                f"""
                create keyspace if not exists {keyspace}
                with replication = {{
                    'class': 'SimpleStrategy',
                    'replication_factor': {replication_factor}
                }}
                """
            )
            session.set_keyspace(keyspace)
            session.execute(
                f"""
                create table if not exists {table} (
                    scope text primary key,
                    published_at bigint,
                    origin_participant text,
                    state_hash text,
                    payload text
                )
                """
            )
            self.rbac_sync_cassandra_cluster = cluster
            self.rbac_sync_cassandra_session = session
            return session

    def read_shared_rbac_state(self):
        session = self.ensure_rbac_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"select scope, published_at, origin_participant, state_hash, payload "
                f"from {self.rbac_sync_cassandra_table} where scope = %s"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        row = session.execute(statement, (self.rbac_sync_scope,)).one()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload or "{}")
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"invalid shared RBAC sync payload: {error}") from error
        payload_hash = ((payload or {}).get("hash") or "").strip()
        expected_hash = (row.state_hash or "").strip()
        if expected_hash and payload_hash and payload_hash != expected_hash:
            raise RuntimeError(
                "shared RBAC sync payload hash mismatch: "
                f"expected {expected_hash}, got {payload_hash}"
            )
        return {
            "scope": (row.scope or "").strip(),
            "publishedAt": int(row.published_at or 0),
            "originParticipant": (row.origin_participant or "").strip(),
            "stateHash": expected_hash or payload_hash,
            "state": payload if isinstance(payload, dict) else {},
        }

    def wait_for_shared_rbac_state(self, received_published_at=0, timeout_seconds=15):
        deadline = time.time() + max(1, int(timeout_seconds or 0))
        latest = None
        while True:
            latest = self.read_shared_rbac_state()
            if latest is None:
                if time.time() >= deadline:
                    return None
            else:
                shared_published_at = int(latest.get("publishedAt") or 0)
                if not received_published_at or shared_published_at >= received_published_at:
                    return latest
                if time.time() >= deadline:
                    raise RuntimeError(
                        "shared RBAC sync state is older than the received intent: "
                        f"{shared_published_at} < {received_published_at}"
                    )
            time.sleep(1)

    def upsert_shared_rbac_state(self, state, published_at):
        state_hash = ((state or {}).get("hash") or "").strip()
        if not state_hash:
            raise RuntimeError("RBAC sync state is missing a hash")
        session = self.ensure_rbac_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"insert into {self.rbac_sync_cassandra_table} "
                "(scope, published_at, origin_participant, state_hash, payload) "
                "values (%s, %s, %s, %s, %s)"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        session.execute(
            statement,
            (
                self.rbac_sync_scope,
                int(published_at or time.time()),
                self.pod_name,
                state_hash,
                stable_json(state),
            ),
        )

    def publish_rbac_state_now(self):
        if not self.rbac_sync_enabled:
            return

        state = self.read_local_rbac_state()
        now = int(time.time())
        state_hash = (state.get("hash") or "").strip()
        snapshot = self.rbac_sync_state_snapshot()
        version_at = max(int(snapshot.get("desiredPublishedAt") or 0), now)
        if (snapshot.get("desiredHash") or "").strip() != state_hash:
            version_at = now

        self.upsert_shared_rbac_state(state, version_at)
        payload = self.build_rbac_state_payload(state, version_at)
        self.publish_envelope(
            f"relay.rbac-state.{sanitize_fragment(self.pod_name)}",
            payload,
        )
        self.last_published_rbac_hash = state_hash
        self.next_rbac_publish = now + self.publish_interval
        self.record_published_rbac_state(state, version_at, now)

    def publish_rbac_state(self):
        if (
            not self.rbac_sync_enabled
            or self.connection is None
            or self.channel is None
            or self.bundle is None
        ):
            return

        state = self.read_local_rbac_state()
        now = int(time.time())
        state_hash = (state.get("hash") or "").strip()
        should_publish = state_hash != self.last_published_rbac_hash or now >= self.next_rbac_publish
        if not should_publish:
            return

        snapshot = self.rbac_sync_state_snapshot()
        version_at = max(int(snapshot.get("desiredPublishedAt") or 0), now)
        if (snapshot.get("desiredHash") or "").strip() != state_hash:
            version_at = now

        self.upsert_shared_rbac_state(state, version_at)
        payload = self.build_rbac_state_payload(state, version_at)
        self.channel.basic_publish(
            exchange=self.bundle["hub"]["exchanges"]["data"],
            routing_key=f"relay.rbac-state.{sanitize_fragment(self.pod_name)}",
            body=stable_json(payload).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )
        self.last_published_rbac_hash = state_hash
        self.next_rbac_publish = now + self.publish_interval
        self.record_published_rbac_state(state, version_at, now)

    def rbac_managed_tokens_delete_sql(self):
        if not self.rbac_sync_excluded_token_label_prefixes:
            return "delete from tokens"
        conditions = [
            "coalesce(label, '') like '" + prefix.replace("'", "''") + "%'"
            for prefix in self.rbac_sync_excluded_token_label_prefixes
        ]
        return "delete from tokens where not (" + " or ".join(conditions) + ")"

    def write_managed_file(self, path, content, reference_path, default_mode):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        reference = None
        for candidate in [path, reference_path]:
            if candidate and os.path.exists(candidate):
                reference = candidate
                break
        if reference is not None:
            ref_stat = os.stat(reference)
            uid = ref_stat.st_uid
            gid = ref_stat.st_gid
            mode = stat.S_IMODE(ref_stat.st_mode)
        else:
            uid = os.getuid()
            gid = os.getgid()
            mode = default_mode
        temp_path = f"{path}.tmp-{uuid.uuid4().hex}"
        try:
            with open(temp_path, "wb") as handle:
                handle.write(content)
            os.chown(temp_path, uid, gid)
            os.chmod(temp_path, mode)
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def apply_rbac_auth_files(self, auth_files):
        current_paths, target_paths = self.rbac_sync_auth_file_paths()
        changed = False
        for logical_name, encoded in (auth_files or {}).items():
            target_path = target_paths.get(logical_name)
            if not target_path:
                continue
            content = base64.b64decode(encoded.encode("ascii"))
            current_content = b""
            if os.path.isfile(target_path):
                with open(target_path, "rb") as handle:
                    current_content = handle.read()
            if current_content == content:
                continue
            default_mode = 0o400 if logical_name.endswith("Key") else 0o644
            self.write_managed_file(
                target_path,
                content,
                current_paths.get(logical_name),
                default_mode,
            )
            changed = True
        return changed

    def upsert_rbac_subjects(self, cursor, rows):
        payload = json.dumps(rows, separators=(",", ":"), sort_keys=True)
        cursor.execute(
            """
            delete from subjects
            where id not in (
                select id
                from json_to_recordset(%s::json) as x(id uuid)
            )
            """,
            (payload,),
        )
        if not rows:
            return
        cursor.execute(
            """
            insert into subjects (
                id,
                login,
                is_group,
                is_remote,
                is_superuser,
                display_name,
                email,
                is_revoked,
                password,
                reset_password_uuid,
                failed_login_attempts,
                created_at,
                external_access_id,
                is_immutable
            )
            select
                id,
                login,
                is_group,
                is_remote,
                is_superuser,
                display_name,
                email,
                is_revoked,
                password,
                reset_password_uuid,
                failed_login_attempts,
                created_at,
                external_access_id,
                is_immutable
            from json_to_recordset(%s::json) as x(
                id uuid,
                login text,
                is_group boolean,
                is_remote boolean,
                is_superuser boolean,
                display_name text,
                email text,
                is_revoked boolean,
                last_login timestamp without time zone,
                password text,
                reset_password_uuid uuid,
                failed_login_attempts integer,
                created_at timestamp without time zone,
                external_access_id uuid,
                is_immutable boolean
            )
            on conflict (id) do update
            set
                login = excluded.login,
                is_group = excluded.is_group,
                is_remote = excluded.is_remote,
                is_superuser = excluded.is_superuser,
                display_name = excluded.display_name,
                email = excluded.email,
                is_revoked = excluded.is_revoked,
                password = excluded.password,
                reset_password_uuid = excluded.reset_password_uuid,
                failed_login_attempts = excluded.failed_login_attempts,
                created_at = excluded.created_at,
                external_access_id = excluded.external_access_id,
                is_immutable = excluded.is_immutable
            """,
            (payload,),
        )

    def rbac_protected_subject_role_rows(self, cursor):
        cursor.execute(
            """
            select id, 1 as rid
            from subjects
            where coalesce(is_superuser, false)
              and coalesce(is_immutable, false)
              and login in ('admin', 'api_user')
            order by login, id
            """
        )
        return [{"sid": str(row[0]), "rid": int(row[1])} for row in cursor.fetchall()]

    def ensure_local_rbac_protected_rows(self, cursor):
        protected_rows = self.rbac_protected_subject_role_rows(cursor)
        if not protected_rows:
            return False
        payload = json.dumps(protected_rows, separators=(",", ":"), sort_keys=True)
        cursor.execute(
            """
            insert into subject_roles (sid, rid)
            select sid, rid
            from json_to_recordset(%s::json) as x(
                sid uuid,
                rid integer
            )
            on conflict do nothing
            """,
            (payload,),
        )
        return True

    def replace_rbac_table(self, cursor, table_name, rows):
        payload = json.dumps(rows, separators=(",", ":"), sort_keys=True)
        if table_name == "configuration":
            cursor.execute("delete from configuration")
            if rows:
                cursor.execute(
                    """
                    insert into configuration (id, kind, data, created_at, modified_at)
                    select id, kind, data, created_at, modified_at
                    from json_to_recordset(%s::json) as x(
                        id uuid,
                        kind text,
                        data jsonb,
                        created_at timestamp with time zone,
                        modified_at timestamp with time zone
                    )
                    """,
                    (payload,),
                )
            return
        if table_name == "external_access_config":
            cursor.execute("delete from external_access_config")
            if rows:
                cursor.execute(
                    """
                    insert into external_access_config (
                        id, config_type, display_name, creation_date, last_updated,
                        data_element, secrets, encryption_key_id
                    )
                    select
                        id,
                        config_type,
                        display_name,
                        creation_date,
                        last_updated,
                        data_element,
                        decode(nullif(secrets_base64, ''), 'base64'),
                        encryption_key_id
                    from json_to_recordset(%s::json) as x(
                        id uuid,
                        config_type text,
                        display_name text,
                        creation_date timestamp without time zone,
                        last_updated timestamp without time zone,
                        data_element jsonb,
                        secrets_base64 text,
                        encryption_key_id text
                    )
                    """,
                    (payload,),
                )
            return
        if table_name == "permissions":
            cursor.execute("delete from permissions")
            if rows:
                cursor.execute(
                    """
                    insert into permissions (id, object_type, action, instance)
                    select id, object_type, action, instance
                    from json_to_recordset(%s::json) as x(
                        id uuid,
                        object_type text,
                        action text,
                        instance text
                    )
                    """,
                    (payload,),
                )
            return
        if table_name == "roles":
            cursor.execute("delete from roles")
            if rows:
                cursor.execute(
                    """
                    insert into roles (id, display_name, description)
                    select id, display_name, description
                    from json_to_recordset(%s::json) as x(
                        id integer,
                        display_name text,
                        description text
                    )
                    """,
                    (payload,),
                )
            return
        if table_name == "roles_permissions":
            cursor.execute("delete from roles_permissions")
            if rows:
                cursor.execute(
                    """
                    insert into roles_permissions (rid, pid)
                    select rid, pid
                    from json_to_recordset(%s::json) as x(
                        rid integer,
                        pid uuid
                    )
                    """,
                    (payload,),
                )
            return
        if table_name == "salt":
            cursor.execute("delete from salt")
            if rows:
                cursor.execute(
                    """
                    insert into salt (id, salt)
                    select id, salt
                    from json_to_recordset(%s::json) as x(
                        id integer,
                        salt text
                    )
                    """,
                    (payload,),
                )
            return
        if table_name == "subject_roles":
            protected_rows = self.rbac_protected_subject_role_rows(cursor)
            protected_keys = {
                (str(row.get("sid") or ""), int(row.get("rid") or 0))
                for row in protected_rows
            }
            combined_rows = list(rows)
            existing_keys = {
                (str(row.get("sid") or ""), int(row.get("rid") or 0))
                for row in combined_rows
            }
            for row in protected_rows:
                key = (str(row.get("sid") or ""), int(row.get("rid") or 0))
                if key not in existing_keys:
                    combined_rows.append(row)
                    existing_keys.add(key)
            payload = json.dumps(combined_rows, separators=(",", ":"), sort_keys=True)
            cursor.execute(
                """
                delete from subject_roles
                where sid not in (
                    select id
                    from subjects
                    where coalesce(is_superuser, false) or coalesce(is_immutable, false)
                )
                """
            )
            if combined_rows:
                cursor.execute(
                    """
                    insert into subject_roles (sid, rid)
                    select sid, rid
                    from json_to_recordset(%s::json) as x(
                        sid uuid,
                        rid integer
                    )
                    on conflict do nothing
                    """,
                    (payload,),
                )
            return
        if table_name == "groupings":
            cursor.execute("delete from groupings")
            if rows:
                cursor.execute(
                    """
                    insert into groupings (gid, ruid)
                    select gid, ruid
                    from json_to_recordset(%s::json) as x(
                        gid uuid,
                        ruid uuid
                    )
                    """,
                    (payload,),
                )
            return
        if table_name == "password_history":
            cursor.execute("delete from password_history")
            if rows:
                cursor.execute(
                    """
                    insert into password_history (id, sid, replacement_date, password)
                    select id, sid, replacement_date, password
                    from json_to_recordset(%s::json) as x(
                        id uuid,
                        sid uuid,
                        replacement_date timestamp without time zone,
                        password text
                    )
                    """,
                    (payload,),
                )
            return
        if table_name == "password_reset_tokens":
            cursor.execute("delete from password_reset_tokens")
            if rows:
                cursor.execute(
                    """
                    insert into password_reset_tokens (token, sid, requestor, expiration_date, creation_date)
                    select token, sid, requestor, expiration_date, creation_date
                    from json_to_recordset(%s::json) as x(
                        token text,
                        sid uuid,
                        requestor uuid,
                        expiration_date timestamp without time zone,
                        creation_date timestamp without time zone
                    )
                    """,
                    (payload,),
                )
            return
        if table_name == "tokens":
            normalized_rows = []
            for row in rows:
                normalized_row = dict(row)
                timeout = (normalized_row.get("timeout") or "").strip()
                if timeout and not normalized_row.get("last_active"):
                    normalized_row["last_active"] = normalized_row.get("creation")
                normalized_rows.append(normalized_row)
            payload = json.dumps(normalized_rows, separators=(",", ":"), sort_keys=True)
            cursor.execute(self.rbac_managed_tokens_delete_sql())
            if normalized_rows:
                cursor.execute(
                    """
                    insert into tokens (
                        id, expiration, user_id, token, label, creation, client,
                        description, timeout, last_active, token_hash
                    )
                    select
                        id, expiration, user_id, token, label, creation, client,
                        description, timeout, last_active, token_hash
                    from json_to_recordset(%s::json) as x(
                        id uuid,
                        expiration timestamp with time zone,
                        user_id uuid,
                        token text,
                        label text,
                        creation timestamp with time zone,
                        client text,
                        description text,
                        timeout text,
                        last_active timestamp with time zone,
                        token_hash text
                    )
                    """,
                    (payload,),
                )
            return
        raise RuntimeError(f"unsupported RBAC sync table {table_name}")

    def reconcile_rbac_state(self, state_payload, published_at, origin_participant):
        shared = self.wait_for_shared_rbac_state(published_at)
        if shared is None:
            raise RuntimeError("shared RBAC sync state is missing from Cassandra")
        shared_published_at = int(shared.get("publishedAt") or 0)

        shared_state = dict(shared.get("state") or {})
        state_payload = shared_state
        origin_participant = (shared.get("originParticipant") or "").strip() or origin_participant
        published_at = shared_published_at or int(published_at or time.time())

        desired_tables = {}
        table_names = self.rbac_sync_table_names()
        for table_name in table_names:
            rows = state_payload.get("tables", {}).get(table_name) or []
            if not isinstance(rows, list):
                raise RuntimeError(f"RBAC sync payload table {table_name} is not a list")
            desired_tables[table_name] = rows

        auth_files = dict(state_payload.get("authFiles") or {})
        self.apply_rbac_auth_files(auth_files)

        connection = self.rbac_db_connection()
        try:
            cursor = connection.cursor()
            clear_order = [
                "roles_permissions",
                "groupings",
                "subject_roles",
                "password_reset_tokens",
                "password_history",
                "tokens",
                "configuration",
                "external_access_config",
                "roles",
                "permissions",
            ]
            apply_order = [
                "salt",
                "permissions",
                "roles",
                "configuration",
                "external_access_config",
                "roles_permissions",
                "subject_roles",
                "groupings",
                "password_history",
                "password_reset_tokens",
                "tokens",
            ]
            for table_name in clear_order:
                if table_name in desired_tables:
                    self.replace_rbac_table(cursor, table_name, [])
            if "subjects" in desired_tables:
                self.upsert_rbac_subjects(cursor, desired_tables["subjects"])
            for table_name in apply_order:
                if table_name in desired_tables:
                    self.replace_rbac_table(cursor, table_name, desired_tables[table_name])
            connection.commit()
            cursor.close()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        local_state = self.read_local_rbac_state()
        actual_hash = (local_state.get("hash") or "").strip()
        desired_hash = (state_payload.get("hash") or "").strip()
        updates = {
            "scope": state_payload.get("scope") or self.rbac_sync_scope,
            "excludedTokenLabelPrefixes": list(
                state_payload.get("excludedTokenLabelPrefixes") or self.rbac_sync_excluded_token_label_prefixes
            ),
            "desiredHash": desired_hash,
            "actualHash": actual_hash,
            "desiredTableCount": int(state_payload.get("tableCount") or len(table_names)),
            "actualTableCount": int(local_state.get("tableCount") or 0),
            "desiredRowCount": int(state_payload.get("rowCount") or 0),
            "actualRowCount": int(local_state.get("rowCount") or 0),
            "authFileCount": int(local_state.get("authFileCount") or 0),
            "originParticipant": origin_participant,
            "desiredPublishedAt": int(published_at or time.time()),
            "lastAppliedAt": int(time.time()),
        }
        if actual_hash == desired_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": int(time.time()),
                    "lastError": "",
                }
            )
        else:
            updates.update(
                {
                    "state": "failed",
                    "lastError": (
                        "RBAC managed-domain hash mismatch after apply: "
                        f"expected {desired_hash}, got {actual_hash or 'none'}"
                    ),
                }
            )
        self.rbac_sync_runtime_error = ""
        self.merge_rbac_sync_state(**updates)

    def handle_remote_rbac_state(self, payload):
        if not self.rbac_sync_enabled:
            return

        origin = payload.get("origin") or {}
        origin_participant = (origin.get("participant") or "").strip()
        if not origin_participant or origin_participant == self.pod_name:
            return

        target_roles = payload.get("targetRoles") or []
        if target_roles and self.relay_role not in target_roles:
            return

        state_payload = payload.get("state") or {}
        desired_hash = (state_payload.get("hash") or "").strip()
        if not desired_hash:
            raise RuntimeError("RBAC sync payload is missing a hash")

        published_at = int(payload.get("publishedAt") or 0)
        shared = self.wait_for_shared_rbac_state(published_at)
        if shared is None:
            raise RuntimeError("shared RBAC sync state is missing from Cassandra")
        shared_hash = (shared.get("stateHash") or "").strip()
        shared_published_at = int(shared.get("publishedAt") or 0)
        if shared_hash:
            desired_hash = shared_hash
        published_at = shared_published_at or published_at
        origin_participant = (
            (shared.get("originParticipant") or "").strip() or origin_participant
        )
        shared_payload = shared.get("state") or {}
        if isinstance(shared_payload, dict):
            state_payload = shared_payload
        current_state = self.rbac_sync_state_snapshot()
        current_desired_hash = (current_state.get("desiredHash") or "").strip()
        current_actual_hash = (current_state.get("actualHash") or "").strip()
        current_published_at = int(current_state.get("desiredPublishedAt") or 0)
        if current_published_at and published_at and published_at < current_published_at:
            return
        if (
            current_published_at
            and published_at
            and published_at == current_published_at
            and desired_hash == current_desired_hash
        ):
            return
        if desired_hash == current_actual_hash and desired_hash == current_desired_hash:
            self.merge_rbac_sync_state(
                state="converged",
                lastReceivedAt=int(time.time()),
                lastConvergedAt=int(time.time()),
                lastError="",
            )
            return

        self.merge_rbac_sync_state(
            state="pending",
            scope=state_payload.get("scope") or self.rbac_sync_scope,
            excludedTokenLabelPrefixes=list(
                state_payload.get("excludedTokenLabelPrefixes") or self.rbac_sync_excluded_token_label_prefixes
            ),
            desiredHash=desired_hash,
            desiredTableCount=int(state_payload.get("tableCount") or len(self.rbac_sync_table_names())),
            desiredRowCount=int(state_payload.get("rowCount") or 0),
            authFileCount=int(state_payload.get("authFileCount") or 0),
            originParticipant=origin_participant,
            desiredPublishedAt=published_at or int(time.time()),
            lastReceivedAt=int(time.time()),
            lastError="",
        )
        log(
            "Received RBAC sync intent at "
            f"{desired_hash[:12]} from {origin_participant}"
        )
        self.reconcile_rbac_state(state_payload, published_at, origin_participant)

    def read_local_rbac_token_state(self):
        current_paths, target_paths = self.rbac_sync_auth_file_paths()
        auth_files = {}
        for logical_name, source_path in {**RBAC_SYNC_REQUIRED_AUTH_FILES, **RBAC_SYNC_OPTIONAL_AUTH_FILES}.items():
            if logical_name not in self.rbac_token_sync_auth_file_names():
                continue
            actual_path = target_paths.get(logical_name)
            if logical_name != "keysJson" and actual_path and os.path.isfile(actual_path):
                source_path = actual_path
            else:
                source_path = current_paths.get(logical_name) or source_path
            if not source_path or not os.path.isfile(source_path):
                if logical_name in RBAC_SYNC_OPTIONAL_AUTH_FILES:
                    continue
                raise RuntimeError(f"RBAC token auth file is missing: {source_path or logical_name}")
            with open(source_path, "rb") as handle:
                auth_files[logical_name] = base64.b64encode(handle.read()).decode("ascii")

        connection = self.rbac_db_connection()
        table_rows = {}
        try:
            cursor = connection.cursor()
            rows = self.rbac_query_rows(cursor, RBAC_SYNC_SELECT_QUERIES["tokens"])
            table_rows["tokens"] = [row for row in rows if not self.rbac_token_is_excluded(row)]
            cursor.close()
        finally:
            connection.close()

        row_count = len(table_rows["tokens"])
        payload = {
            "scope": self.rbac_token_sync_scope,
            "excludedTokenLabelPrefixes": list(self.rbac_sync_excluded_token_label_prefixes),
            "tableNames": list(RBAC_TOKEN_SYNC_TABLE_NAMES),
            "tables": table_rows,
            "tableCount": len(RBAC_TOKEN_SYNC_TABLE_NAMES),
            "rowCount": row_count,
            "authFiles": auth_files,
            "authFileCount": len(auth_files),
        }
        payload["hash"] = self.rbac_rows_hash(
            table_rows,
            auth_files,
            self.rbac_sync_excluded_token_label_prefixes,
        )
        return payload

    def refresh_local_rbac_token_sync_state(self):
        if not self.rbac_token_sync_enabled:
            return

        observed_at = int(time.time())
        local_state = self.read_local_rbac_token_state()
        snapshot = self.rbac_token_sync_state_snapshot()
        desired_hash = (snapshot.get("desiredHash") or "").strip()
        actual_hash = (local_state.get("hash") or "").strip()
        phase = (snapshot.get("state") or "idle").strip() or "idle"
        origin_participant = (snapshot.get("originParticipant") or "").strip()

        updates = {
            "scope": self.rbac_token_sync_scope,
            "excludedTokenLabelPrefixes": list(self.rbac_sync_excluded_token_label_prefixes),
            "actualHash": actual_hash,
            "actualTableCount": int(local_state.get("tableCount") or 0),
            "actualRowCount": int(local_state.get("rowCount") or 0),
            "authFileCount": int(local_state.get("authFileCount") or 0),
        }
        if desired_hash and desired_hash == actual_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        elif desired_hash and origin_participant and origin_participant != self.pod_name and phase in {
            "pending",
            "in-progress",
            "failed",
        }:
            updates["state"] = phase
        else:
            updates.update(
                {
                    "state": "observed",
                    "desiredHash": actual_hash,
                    "desiredTableCount": int(local_state.get("tableCount") or 0),
                    "desiredRowCount": int(local_state.get("rowCount") or 0),
                    "originParticipant": self.pod_name,
                    "desiredPublishedAt": observed_at,
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        self.rbac_token_sync_runtime_error = ""
        self.merge_rbac_token_sync_state(**updates)

    def build_rbac_token_state_payload(self, state, published_at):
        return {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorRelayRbacTokenState",
            "publishedAt": published_at,
            "origin": {
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "role": self.relay_role,
                "segment": self.segment_name,
            },
            "targetRoles": list(self.rbac_token_sync_target_roles),
            "state": {
                "scope": state.get("scope", self.rbac_token_sync_scope),
                "excludedTokenLabelPrefixes": list(self.rbac_sync_excluded_token_label_prefixes),
                "hash": state.get("hash", ""),
                "tableCount": int(state.get("tableCount") or 0),
                "rowCount": int(state.get("rowCount") or 0),
                "authFileCount": int(state.get("authFileCount") or 0),
            },
        }

    def record_published_rbac_token_state(self, state, published_at, now):
        state_hash = (state.get("hash") or "").strip()
        self.merge_rbac_token_sync_state(
            state="converged",
            scope=self.rbac_token_sync_scope,
            excludedTokenLabelPrefixes=list(self.rbac_sync_excluded_token_label_prefixes),
            desiredHash=state_hash,
            actualHash=state_hash,
            desiredTableCount=int(state.get("tableCount") or 0),
            actualTableCount=int(state.get("tableCount") or 0),
            desiredRowCount=int(state.get("rowCount") or 0),
            actualRowCount=int(state.get("rowCount") or 0),
            authFileCount=int(state.get("authFileCount") or 0),
            originParticipant=self.pod_name,
            desiredPublishedAt=published_at,
            lastPublishedAt=now,
            lastConvergedAt=now,
            lastError="",
        )

    def ensure_rbac_token_sync_cassandra_session(self):
        if not self.rbac_token_sync_enabled:
            raise RuntimeError("RBAC token sync is not enabled")
        if not self.rbac_token_sync_cassandra_contact_points:
            raise RuntimeError("no Cassandra contact points configured for RBAC token sync")
        if Cluster is None or ConsistencyLevel is None or SimpleStatement is None:
            raise RuntimeError("cassandra-driver is not available in the Conductor image")

        with self.lock:
            if self.rbac_token_sync_cassandra_session is not None:
                return self.rbac_token_sync_cassandra_session

            cluster = Cluster(
                contact_points=self.rbac_token_sync_cassandra_contact_points,
                port=self.rbac_token_sync_cassandra_port,
            )
            session = cluster.connect()
            keyspace = self.rbac_token_sync_cassandra_keyspace
            table = self.rbac_token_sync_cassandra_table
            replication_factor = max(1, self.rbac_token_sync_cassandra_replication_factor)
            session.execute(
                f"""
                create keyspace if not exists {keyspace}
                with replication = {{
                    'class': 'SimpleStrategy',
                    'replication_factor': {replication_factor}
                }}
                """
            )
            session.set_keyspace(keyspace)
            session.execute(
                f"""
                create table if not exists {table} (
                    scope text primary key,
                    published_at bigint,
                    origin_participant text,
                    state_hash text,
                    payload text
                )
                """
            )
            self.rbac_token_sync_cassandra_cluster = cluster
            self.rbac_token_sync_cassandra_session = session
            return session

    def read_shared_rbac_token_state(self):
        session = self.ensure_rbac_token_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"select scope, published_at, origin_participant, state_hash, payload "
                f"from {self.rbac_token_sync_cassandra_table} where scope = %s"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        row = session.execute(statement, (self.rbac_token_sync_scope,)).one()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload or "{}")
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"invalid shared RBAC token sync payload: {error}") from error
        payload_hash = ((payload or {}).get("hash") or "").strip()
        expected_hash = (row.state_hash or "").strip()
        if expected_hash and payload_hash and payload_hash != expected_hash:
            raise RuntimeError(
                "shared RBAC token sync payload hash mismatch: "
                f"expected {expected_hash}, got {payload_hash}"
            )
        return {
            "scope": (row.scope or "").strip(),
            "publishedAt": int(row.published_at or 0),
            "originParticipant": (row.origin_participant or "").strip(),
            "stateHash": expected_hash or payload_hash,
            "state": payload if isinstance(payload, dict) else {},
        }

    def wait_for_shared_rbac_token_state(self, received_published_at=0, timeout_seconds=15):
        deadline = time.time() + max(1, int(timeout_seconds or 0))
        while True:
            shared = self.read_shared_rbac_token_state()
            if shared is None:
                if time.time() >= deadline:
                    return None
            else:
                shared_published_at = int(shared.get("publishedAt") or 0)
                if not received_published_at or shared_published_at >= received_published_at:
                    return shared
                if time.time() >= deadline:
                    raise RuntimeError(
                        "shared RBAC token sync state is older than the received intent: "
                        f"{shared_published_at} < {received_published_at}"
                    )
            time.sleep(1)

    def upsert_shared_rbac_token_state(self, state, published_at):
        state_hash = ((state or {}).get("hash") or "").strip()
        if not state_hash:
            raise RuntimeError("RBAC token sync state is missing a hash")
        session = self.ensure_rbac_token_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"insert into {self.rbac_token_sync_cassandra_table} "
                "(scope, published_at, origin_participant, state_hash, payload) "
                "values (%s, %s, %s, %s, %s)"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        session.execute(
            statement,
            (
                self.rbac_token_sync_scope,
                int(published_at or time.time()),
                self.pod_name,
                state_hash,
                stable_json(state),
            ),
        )

    def publish_rbac_token_state_now(self):
        if not self.rbac_token_sync_enabled:
            return

        state = self.read_local_rbac_token_state()
        now = int(time.time())
        state_hash = (state.get("hash") or "").strip()
        snapshot = self.rbac_token_sync_state_snapshot()
        version_at = max(int(snapshot.get("desiredPublishedAt") or 0), now)
        if (snapshot.get("desiredHash") or "").strip() != state_hash:
            version_at = now

        self.upsert_shared_rbac_token_state(state, version_at)
        payload = self.build_rbac_token_state_payload(state, version_at)
        self.publish_envelope(
            f"relay.rbac-token-state.{sanitize_fragment(self.pod_name)}",
            payload,
        )
        self.last_published_rbac_token_hash = state_hash
        self.next_rbac_token_publish = now + self.publish_interval
        self.record_published_rbac_token_state(state, version_at, now)

    def publish_rbac_token_state(self):
        if (
            not self.rbac_token_sync_enabled
            or self.connection is None
            or self.channel is None
            or self.bundle is None
        ):
            return

        state = self.read_local_rbac_token_state()
        now = int(time.time())
        state_hash = (state.get("hash") or "").strip()
        should_publish = (
            state_hash != self.last_published_rbac_token_hash
            or now >= self.next_rbac_token_publish
        )
        if not should_publish:
            return

        snapshot = self.rbac_token_sync_state_snapshot()
        version_at = max(int(snapshot.get("desiredPublishedAt") or 0), now)
        if (snapshot.get("desiredHash") or "").strip() != state_hash:
            version_at = now

        self.upsert_shared_rbac_token_state(state, version_at)
        payload = self.build_rbac_token_state_payload(state, version_at)
        self.channel.basic_publish(
            exchange=self.bundle["hub"]["exchanges"]["data"],
            routing_key=f"relay.rbac-token-state.{sanitize_fragment(self.pod_name)}",
            body=stable_json(payload).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )
        self.last_published_rbac_token_hash = state_hash
        self.next_rbac_token_publish = now + self.publish_interval
        self.record_published_rbac_token_state(state, version_at, now)

    def local_rbac_token_exists(self, token):
        value = (token or "").strip()
        if not value:
            return False
        connection = self.rbac_db_connection()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    select 1
                    from tokens
                    where token = %s
                    limit 1
                    """,
                    (value,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        finally:
            connection.close()
        return row is not None

    def rbac_token_in_state(self, token, state_payload):
        value = (token or "").strip()
        if not value:
            return False
        for row in ((state_payload or {}).get("tables") or {}).get("tokens") or []:
            if ((row or {}).get("token") or "").strip() == value:
                return True
        return False

    def reconcile_rbac_token_state(self, state_payload, published_at, origin_participant):
        shared = self.wait_for_shared_rbac_token_state(published_at)
        if shared is None:
            raise RuntimeError("shared RBAC token sync state is missing from Cassandra")
        shared_published_at = int(shared.get("publishedAt") or 0)

        shared_state = dict(shared.get("state") or {})
        state_payload = shared_state
        origin_participant = (shared.get("originParticipant") or "").strip() or origin_participant
        published_at = shared_published_at or int(published_at or time.time())
        auth_files = dict(shared_state.get("authFiles") or {})
        self.apply_rbac_auth_files(auth_files)

        desired_tables = {}
        for table_name in RBAC_TOKEN_SYNC_TABLE_NAMES:
            rows = shared_state.get("tables", {}).get(table_name) or []
            if not isinstance(rows, list):
                raise RuntimeError(f"RBAC token sync payload table {table_name} is not a list")
            desired_tables[table_name] = rows

        connection = self.rbac_db_connection()
        try:
            cursor = connection.cursor()
            for table_name in RBAC_TOKEN_SYNC_TABLE_NAMES:
                self.replace_rbac_table(cursor, table_name, desired_tables[table_name])
            connection.commit()
            cursor.close()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        local_state = self.read_local_rbac_token_state()
        actual_hash = (local_state.get("hash") or "").strip()
        desired_hash = (shared_state.get("hash") or state_payload.get("hash") or "").strip()
        updates = {
            "scope": state_payload.get("scope") or self.rbac_token_sync_scope,
            "excludedTokenLabelPrefixes": list(
                state_payload.get("excludedTokenLabelPrefixes") or self.rbac_sync_excluded_token_label_prefixes
            ),
            "desiredHash": desired_hash,
            "actualHash": actual_hash,
            "desiredTableCount": int(state_payload.get("tableCount") or len(RBAC_TOKEN_SYNC_TABLE_NAMES)),
            "actualTableCount": int(local_state.get("tableCount") or 0),
            "desiredRowCount": int(state_payload.get("rowCount") or 0),
            "actualRowCount": int(local_state.get("rowCount") or 0),
            "authFileCount": int(local_state.get("authFileCount") or 0),
            "originParticipant": origin_participant,
            "desiredPublishedAt": int(published_at or time.time()),
            "lastAppliedAt": int(time.time()),
        }
        if actual_hash == desired_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": int(time.time()),
                    "lastError": "",
                }
            )
        else:
            updates.update(
                {
                    "state": "failed",
                    "lastError": (
                        "RBAC token managed-domain hash mismatch after apply: "
                        f"expected {desired_hash}, got {actual_hash or 'none'}"
                    ),
                }
            )
        self.rbac_token_sync_runtime_error = ""
        self.merge_rbac_token_sync_state(**updates)

    def ensure_local_rbac_token_from_shared_state(self, token):
        if self.local_rbac_token_exists(token):
            return True
        if not self.rbac_token_sync_enabled:
            return False
        shared = self.read_shared_rbac_token_state()
        if shared is None:
            return False
        shared_state = shared.get("state") or {}
        if not self.rbac_token_in_state(token, shared_state):
            return False
        self.reconcile_rbac_token_state(
            shared_state,
            int(shared.get("publishedAt") or time.time()),
            (shared.get("originParticipant") or "").strip(),
        )
        return self.local_rbac_token_exists(token)

    def handle_remote_rbac_token_state(self, payload):
        if not self.rbac_token_sync_enabled:
            return

        origin = payload.get("origin") or {}
        origin_participant = (origin.get("participant") or "").strip()
        if not origin_participant or origin_participant == self.pod_name:
            return

        target_roles = payload.get("targetRoles") or []
        if target_roles and self.relay_role not in target_roles:
            return

        state_payload = payload.get("state") or {}
        desired_hash = (state_payload.get("hash") or "").strip()
        if not desired_hash:
            raise RuntimeError("RBAC token sync payload is missing a hash")

        published_at = int(payload.get("publishedAt") or 0)
        shared = self.wait_for_shared_rbac_token_state(published_at)
        if shared is None:
            raise RuntimeError("shared RBAC token sync state is missing from Cassandra")
        shared_hash = (shared.get("stateHash") or "").strip()
        shared_published_at = int(shared.get("publishedAt") or 0)
        if shared_hash:
            desired_hash = shared_hash
        published_at = shared_published_at or published_at
        origin_participant = (shared.get("originParticipant") or "").strip() or origin_participant
        shared_payload = shared.get("state") or {}
        if isinstance(shared_payload, dict):
            state_payload = shared_payload
        current_state = self.rbac_token_sync_state_snapshot()
        current_desired_hash = (current_state.get("desiredHash") or "").strip()
        current_actual_hash = (current_state.get("actualHash") or "").strip()
        current_published_at = int(current_state.get("desiredPublishedAt") or 0)
        if current_published_at and published_at and published_at < current_published_at:
            return
        if (
            current_published_at
            and published_at
            and published_at == current_published_at
            and desired_hash == current_desired_hash
        ):
            return
        if desired_hash == current_actual_hash and desired_hash == current_desired_hash:
            self.merge_rbac_token_sync_state(
                state="converged",
                lastReceivedAt=int(time.time()),
                lastConvergedAt=int(time.time()),
                lastError="",
            )
            return

        self.merge_rbac_token_sync_state(
            state="pending",
            scope=state_payload.get("scope") or self.rbac_token_sync_scope,
            excludedTokenLabelPrefixes=list(
                state_payload.get("excludedTokenLabelPrefixes") or self.rbac_sync_excluded_token_label_prefixes
            ),
            desiredHash=desired_hash,
            desiredTableCount=int(state_payload.get("tableCount") or len(RBAC_TOKEN_SYNC_TABLE_NAMES)),
            desiredRowCount=int(state_payload.get("rowCount") or 0),
            authFileCount=int(state_payload.get("authFileCount") or 0),
            originParticipant=origin_participant,
            desiredPublishedAt=published_at or int(time.time()),
            lastReceivedAt=int(time.time()),
            lastError="",
        )
        log(
            "Received RBAC token sync intent at "
            f"{desired_hash[:12]} from {origin_participant}"
        )
        self.reconcile_rbac_token_state(state_payload, published_at, origin_participant)

    def default_orchestration_sync_state(self):
        return {
            "state": "idle",
            "scope": self.orchestration_sync_scope,
            "desiredHash": "",
            "actualHash": "",
            "desiredDatabaseCount": 0,
            "actualDatabaseCount": 0,
            "desiredTableCount": 0,
            "actualTableCount": 0,
            "desiredRowCount": 0,
            "actualRowCount": 0,
            "authFileCount": 0,
            "sequenceStride": self.orchestration_sync_sequence_stride,
            "sequenceResidue": self.orchestration_sync_sequence_residue,
            "sequenceCount": 0,
            "originParticipant": "",
            "desiredPublishedAt": 0,
            "lastReceivedAt": 0,
            "lastPublishedAt": 0,
            "lastAppliedAt": 0,
            "lastConvergedAt": 0,
            "lastError": "",
            "lastErrorAt": 0,
        }

    def refresh_orchestration_summary_locked(self):
        state = dict(self.orchestration_sync_state)
        ready = not self.orchestration_sync_enabled
        if self.orchestration_sync_enabled:
            desired_hash = (state.get("desiredHash") or "").strip()
            actual_hash = (state.get("actualHash") or "").strip()
            phase = (state.get("state") or "idle").strip() or "idle"
            ready = bool(actual_hash) and phase not in {"pending", "in-progress", "failed"}
            if desired_hash and actual_hash != desired_hash:
                ready = False
            if self.orchestration_sync_runtime_error:
                ready = False

        last_error = self.orchestration_sync_runtime_error or (state.get("lastError") or "").strip()
        self.status.update(
            {
                "orchestrationSyncReady": ready,
                "orchestrationSyncBackend": self.orchestration_sync_backend,
                "orchestrationSyncTargetRoles": list(self.orchestration_sync_target_roles),
                "orchestrationSyncScope": state.get("scope", ""),
                "orchestrationSyncState": state.get("state", "idle"),
                "orchestrationSyncDesiredHash": state.get("desiredHash", ""),
                "orchestrationSyncActualHash": state.get("actualHash", ""),
                "orchestrationSyncDesiredDatabaseCount": int(state.get("desiredDatabaseCount") or 0),
                "orchestrationSyncActualDatabaseCount": int(state.get("actualDatabaseCount") or 0),
                "orchestrationSyncDesiredTableCount": int(state.get("desiredTableCount") or 0),
                "orchestrationSyncActualTableCount": int(state.get("actualTableCount") or 0),
                "orchestrationSyncDesiredRowCount": int(state.get("desiredRowCount") or 0),
                "orchestrationSyncActualRowCount": int(state.get("actualRowCount") or 0),
                "orchestrationSyncAuthFileCount": int(state.get("authFileCount") or 0),
                "orchestrationSyncSequenceStride": int(state.get("sequenceStride") or 0),
                "orchestrationSyncSequenceResidue": int(state.get("sequenceResidue") or 0),
                "orchestrationSyncSequenceCount": int(state.get("sequenceCount") or 0),
                "orchestrationSyncLastPublishedAt": int(state.get("lastPublishedAt") or 0),
                "orchestrationSyncLastAppliedAt": int(state.get("lastAppliedAt") or 0),
                "orchestrationSyncLastConvergedAt": int(state.get("lastConvergedAt") or 0),
                "orchestrationSyncLastError": last_error,
            }
        )

    def refresh_orchestration_summary(self):
        with self.lock:
            self.refresh_orchestration_summary_locked()

    def default_inventory_sync_state(self):
        return {
            "state": "idle",
            "scope": self.inventory_sync_scope,
            "desiredHash": "",
            "actualHash": "",
            "desiredTableCount": 0,
            "actualTableCount": 0,
            "desiredRowCount": 0,
            "actualRowCount": 0,
            "authFileCount": 0,
            "originParticipant": "",
            "desiredPublishedAt": 0,
            "lastReceivedAt": 0,
            "lastPublishedAt": 0,
            "lastAppliedAt": 0,
            "lastConvergedAt": 0,
            "lastError": "",
            "lastErrorAt": 0,
        }

    def refresh_inventory_summary_locked(self):
        state = dict(self.inventory_sync_state)
        ready = not self.inventory_sync_enabled
        if self.inventory_sync_enabled:
            desired_hash = (state.get("desiredHash") or "").strip()
            actual_hash = (state.get("actualHash") or "").strip()
            phase = (state.get("state") or "idle").strip() or "idle"
            ready = bool(actual_hash) and phase not in {"pending", "in-progress", "failed"}
            if desired_hash and actual_hash != desired_hash:
                ready = False
            if self.inventory_sync_runtime_error:
                ready = False

        last_error = self.inventory_sync_runtime_error or (state.get("lastError") or "").strip()
        self.status.update(
            {
                "inventorySyncReady": ready,
                "inventorySyncTargetRoles": list(self.inventory_sync_target_roles),
                "inventorySyncScope": state.get("scope", ""),
                "inventorySyncState": state.get("state", "idle"),
                "inventorySyncDesiredHash": state.get("desiredHash", ""),
                "inventorySyncActualHash": state.get("actualHash", ""),
                "inventorySyncDesiredTableCount": int(state.get("desiredTableCount") or 0),
                "inventorySyncActualTableCount": int(state.get("actualTableCount") or 0),
                "inventorySyncDesiredRowCount": int(state.get("desiredRowCount") or 0),
                "inventorySyncActualRowCount": int(state.get("actualRowCount") or 0),
                "inventorySyncAuthFileCount": int(state.get("authFileCount") or 0),
                "inventorySyncLastPublishedAt": int(state.get("lastPublishedAt") or 0),
                "inventorySyncLastAppliedAt": int(state.get("lastAppliedAt") or 0),
                "inventorySyncLastConvergedAt": int(state.get("lastConvergedAt") or 0),
                "inventorySyncLastError": last_error,
            }
        )

    def refresh_inventory_summary(self):
        with self.lock:
            self.refresh_inventory_summary_locked()

    def persist_orchestration_sync_state(self):
        if not self.orchestration_sync_enabled:
            return
        with self.lock:
            payload = {
                "apiVersion": "pe-k8s.puppet.com/v1alpha1",
                "kind": "ConductorRelayOrchestrationSyncState",
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "state": dict(self.orchestration_sync_state),
            }
        write_text_file(
            self.orchestration_sync_state_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def load_orchestration_sync_state(self):
        state = self.default_orchestration_sync_state()
        if self.orchestration_sync_enabled and os.path.isfile(self.orchestration_sync_state_path):
            try:
                payload = read_json_file(self.orchestration_sync_state_path)
                raw_state = payload.get("state") or {}
                if isinstance(raw_state, dict):
                    state.update(raw_state)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                log(f"Failed to load orchestration sync state: {error}")
        with self.lock:
            self.orchestration_sync_state = state
            self.refresh_orchestration_summary_locked()

    def orchestration_sync_state_snapshot(self):
        with self.lock:
            return dict(self.orchestration_sync_state)

    def merge_orchestration_sync_state(self, **updates):
        with self.lock:
            state = dict(self.orchestration_sync_state)
            state.update(updates)
            if "lastError" in updates:
                if (updates.get("lastError") or "").strip():
                    state["lastErrorAt"] = int(updates.get("lastErrorAt") or time.time())
                else:
                    state["lastErrorAt"] = 0
                    self.orchestration_sync_runtime_error = ""
            self.orchestration_sync_state = state
            self.refresh_orchestration_summary_locked()
            snapshot = dict(state)
        self.persist_orchestration_sync_state()
        return snapshot

    def persist_inventory_sync_state(self):
        if not self.inventory_sync_enabled:
            return
        with self.lock:
            payload = {
                "apiVersion": "pe-k8s.puppet.com/v1alpha1",
                "kind": "ConductorRelayInventorySyncState",
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "state": dict(self.inventory_sync_state),
            }
        write_text_file(self.inventory_sync_state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def load_inventory_sync_state(self):
        state = self.default_inventory_sync_state()
        if self.inventory_sync_enabled and os.path.isfile(self.inventory_sync_state_path):
            try:
                payload = read_json_file(self.inventory_sync_state_path)
                raw_state = payload.get("state") or {}
                if isinstance(raw_state, dict):
                    state.update(raw_state)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                log(f"Failed to load inventory sync state: {error}")
        with self.lock:
            self.inventory_sync_state = state
            self.refresh_inventory_summary_locked()

    def inventory_sync_state_snapshot(self):
        with self.lock:
            return dict(self.inventory_sync_state)

    def merge_inventory_sync_state(self, **updates):
        with self.lock:
            state = dict(self.inventory_sync_state)
            state.update(updates)
            if "lastError" in updates:
                if (updates.get("lastError") or "").strip():
                    state["lastErrorAt"] = int(updates.get("lastErrorAt") or time.time())
                else:
                    state["lastErrorAt"] = 0
                    self.inventory_sync_runtime_error = ""
            self.inventory_sync_state = state
            self.refresh_inventory_summary_locked()
            snapshot = dict(state)
        self.persist_inventory_sync_state()
        return snapshot

    def orchestration_db_conf_path(self, database_name):
        if database_name == "orchestrator":
            return self.orchestration_sync_orchestrator_conf_path
        if database_name == "inventory":
            return self.orchestration_sync_inventory_conf_path
        raise RuntimeError(f"unsupported orchestration sync database {database_name}")

    def orchestration_sync_database_names(self):
        names = list(ORCHESTRATION_SYNC_DATABASE_SPECS)
        if self.inventory_sync_enabled:
            names = [name for name in names if name != "inventory"]
        return names

    def orchestration_sync_auth_file_names(self):
        names = list(ORCHESTRATION_SYNC_REQUIRED_AUTH_FILES)
        if self.inventory_sync_enabled:
            names = [name for name in names if name != "inventoryKeysJson"]
        return names

    def orchestration_sync_uses_cassandra(self):
        return self.orchestration_sync_enabled

    def inventory_sync_auth_file_paths(self):
        inventory_content = ""
        if os.path.isfile(self.inventory_sync_inventory_conf_path):
            with open(self.inventory_sync_inventory_conf_path, "r", encoding="utf-8") as handle:
                inventory_content = handle.read()

        key_dir = self.rbac_sync_hocon_string(inventory_content, "keypath")
        inventory_keys_path = (
            os.path.join(key_dir, "keys.json")
            if key_dir
            else DEFAULT_ORCHESTRATION_SYNC_KEYS_PATH
        )
        return {
            "inventoryKeysJson": inventory_keys_path,
        }

    def orchestration_sync_db_spec(self, database_name):
        spec = ORCHESTRATION_SYNC_DATABASE_SPECS.get(database_name)
        if spec is None:
            raise RuntimeError(f"unsupported orchestration sync database {database_name}")
        return spec

    def orchestration_sync_db_config_from_path(self, conf_path):
        if not os.path.isfile(conf_path):
            raise RuntimeError(f"orchestration database config not found: {conf_path}")
        with open(conf_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        subname = self.rbac_sync_hocon_string(content, "subname")
        user = self.rbac_sync_hocon_string(content, "user")
        migration_user = self.rbac_sync_hocon_string(content, "migration-user")
        if not subname or not user:
            raise RuntimeError(f"orchestration database configuration is incomplete: {conf_path}")
        parsed = urllib.parse.urlsplit(f"postgresql:{subname}")
        params = {
            key: values[-1]
            for key, values in urllib.parse.parse_qs(parsed.query).items()
            if values
        }
        database = parsed.path.lstrip("/")
        sslkey = params.get("sslkey", "")
        if sslkey.endswith(".pk8"):
            pem_key = re.sub(r"\.pk8$", ".pem", sslkey)
            if os.path.isfile(pem_key):
                sslkey = pem_key
        return {
            "host": parsed.hostname or "",
            "port": int(parsed.port or 5432),
            "database": database,
            "user": user,
            "migration_user": migration_user,
            "sslrootcert": params.get("sslrootcert", ""),
            "sslkey": sslkey,
            "sslcert": params.get("sslcert", ""),
        }

    def orchestration_sync_auth_file_paths(self):
        inventory_conf_path = self.orchestration_sync_inventory_conf_path
        orchestrator_conf_path = self.orchestration_sync_orchestrator_conf_path

        inventory_content = ""
        if os.path.isfile(inventory_conf_path):
            with open(inventory_conf_path, "r", encoding="utf-8") as handle:
                inventory_content = handle.read()

        orchestrator_content = ""
        if os.path.isfile(orchestrator_conf_path):
            with open(orchestrator_conf_path, "r", encoding="utf-8") as handle:
                orchestrator_content = handle.read()

        key_dir = self.rbac_sync_hocon_string(inventory_content, "keypath")
        inventory_keys_path = (
            os.path.join(key_dir, "keys.json")
            if key_dir
            else DEFAULT_ORCHESTRATION_SYNC_KEYS_PATH
        )
        orchestrator_encryption_store = (
            self.rbac_sync_hocon_string(orchestrator_content, "encryption-store")
            or DEFAULT_ORCHESTRATION_SYNC_ENCRYPTION_STORE_PATH
        )
        return {
            "inventoryKeysJson": inventory_keys_path,
            "orchestratorEncryptionStore": orchestrator_encryption_store,
        }

    def orchestration_db_connection(self, conf_path, use_migration_user=False):
        config = self.orchestration_sync_db_config_from_path(conf_path)
        username = config["user"]
        if use_migration_user and (config.get("migration_user") or "").strip():
            username = config["migration_user"].strip()
        context = ssl.create_default_context(cafile=config["sslrootcert"])
        context.check_hostname = True
        context.load_cert_chain(
            certfile=config["sslcert"],
            keyfile=config["sslkey"],
        )
        return pg8000.dbapi.connect(
            user=username,
            host=config["host"],
            port=config["port"],
            database=config["database"],
            ssl_context=context,
            timeout=15,
        )

    def orchestration_select_query(self, database_name, table_name):
        table_spec = self.orchestration_sync_db_spec(database_name)["tables"].get(table_name)
        if table_spec is None:
            raise RuntimeError(
                f"unsupported orchestration sync table {database_name}.{table_name}"
            )
        column_names = [name for name, _type_name in table_spec.get("columns") or []]
        if not column_names:
            raise RuntimeError(
                f"orchestration sync table {database_name}.{table_name} has no columns"
            )
        order_names = list(table_spec.get("orderBy") or column_names)
        select_columns = ",\n            ".join(sql_identifier(name) for name in column_names)
        order_columns = ", ".join(sql_identifier(name) for name in order_names)
        return (
            "select\n"
            f"            {select_columns}\n"
            f"        from {sql_identifier(table_name)}\n"
            f"        order by {order_columns}"
        )

    def ensure_orchestration_database_sequences(self, cursor, database_name):
        spec = self.orchestration_sync_db_spec(database_name)
        configured_sequences = []
        for table_name, column_name in spec.get("sequenceColumns") or []:
            cursor.execute(
                "select pg_get_serial_sequence(%s, %s)",
                (table_name, column_name),
            )
            row = cursor.fetchone()
            sequence_name = ((row[0] if row else "") or "").strip()
            if not sequence_name:
                continue
            cursor.execute(
                f"select coalesce(max({sql_identifier(column_name)}), 0) from {sql_identifier(table_name)}"
            )
            max_value = int(cursor.fetchone()[0] or 0)
            cursor.execute(f"select last_value, is_called from {sequence_name}")
            sequence_row = cursor.fetchone()
            if sequence_row is None:
                last_value = 0
                is_called = True
            else:
                last_value = int(sequence_row[0] or 0)
                is_called = bool(sequence_row[1])
            minimum_value = max(
                max_value + 1,
                last_value + (1 if is_called else 0),
                self.orchestration_sync_sequence_residue,
            )
            next_value = sequence_value_for_residue(
                minimum_value,
                self.orchestration_sync_sequence_stride,
                self.orchestration_sync_sequence_residue,
            )
            escaped_sequence_name = sequence_name.replace("'", "''")
            cursor.execute(
                f"alter sequence {sequence_name} "
                f"increment by {self.orchestration_sync_sequence_stride} cache 1"
            )
            cursor.execute(
                f"select setval('{escaped_sequence_name}', {next_value}, false)"
            )
            configured_sequences.append(sequence_name)
        return configured_sequences

    def prepare_orchestration_database_sequences(self, database_name, conf_path):
        spec = self.orchestration_sync_db_spec(database_name)
        if not spec.get("sequenceColumns"):
            return []
        connection = self.orchestration_db_connection(conf_path, use_migration_user=True)
        try:
            cursor = connection.cursor()
            sequence_names = self.ensure_orchestration_database_sequences(cursor, database_name)
            connection.commit()
            cursor.close()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return sequence_names

    def read_orchestration_database_state(self, database_name, conf_path):
        spec = self.orchestration_sync_db_spec(database_name)
        table_names = list(spec.get("tables") or {})
        table_rows = {}
        sequence_names = self.prepare_orchestration_database_sequences(database_name, conf_path)
        connection = self.orchestration_db_connection(conf_path)
        try:
            cursor = connection.cursor()
            cursor.execute("set transaction isolation level repeatable read, read only")
            for table_name in table_names:
                rows = self.rbac_query_rows(
                    cursor,
                    self.orchestration_select_query(database_name, table_name),
                )
                table_rows[table_name] = rows
            cursor.close()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        row_count = sum(len(rows) for rows in table_rows.values())
        return {
            "tableNames": table_names,
            "tables": table_rows,
            "tableCount": len(table_names),
            "rowCount": row_count,
            "sequenceCount": len(sequence_names),
        }

    @staticmethod
    def orchestration_rows_hash(databases, auth_files):
        normalized = {}
        for database_name in ORCHESTRATION_SYNC_DATABASE_SPECS:
            database_state = dict((databases or {}).get(database_name) or {})
            table_names = list(database_state.get("tableNames") or [])
            tables = {
                table_name: list((database_state.get("tables") or {}).get(table_name) or [])
                for table_name in table_names
            }
            normalized[database_name] = {
                "tableNames": table_names,
                "tables": tables,
            }
        return sha256_text(
            stable_json(
                {
                    "databases": normalized,
                    "authFiles": dict(sorted((auth_files or {}).items())),
                }
            )
        )

    def read_local_orchestration_state(self):
        databases = {}
        for database_name in self.orchestration_sync_database_names():
            databases[database_name] = self.read_orchestration_database_state(
                database_name,
                self.orchestration_db_conf_path(database_name),
            )
        auth_files = {}
        auth_paths = self.orchestration_sync_auth_file_paths()
        for logical_name in self.orchestration_sync_auth_file_names():
            source_path = auth_paths.get(logical_name) or ""
            if not source_path or not os.path.isfile(source_path):
                raise RuntimeError(
                    f"orchestration auth file is missing: {source_path or logical_name}"
                )
            with open(source_path, "rb") as handle:
                auth_files[logical_name] = base64.b64encode(handle.read()).decode("ascii")
        database_count = len(databases)
        table_count = sum(int(database.get("tableCount") or 0) for database in databases.values())
        row_count = sum(int(database.get("rowCount") or 0) for database in databases.values())
        sequence_count = sum(
            int(database.get("sequenceCount") or 0) for database in databases.values()
        )
        payload = {
            "scope": self.orchestration_sync_scope,
            "databases": databases,
            "databaseCount": database_count,
            "tableCount": table_count,
            "rowCount": row_count,
            "authFiles": auth_files,
            "authFileCount": len(auth_files),
            "sequenceStride": self.orchestration_sync_sequence_stride,
            "sequenceResidue": self.orchestration_sync_sequence_residue,
            "sequenceCount": sequence_count,
        }
        payload["hash"] = self.orchestration_rows_hash(databases, auth_files)
        return payload

    def refresh_local_orchestration_sync_state(self):
        if not self.orchestration_sync_enabled:
            return

        observed_at = int(time.time())
        local_state = self.read_local_orchestration_state()
        snapshot = self.orchestration_sync_state_snapshot()
        desired_hash = (snapshot.get("desiredHash") or "").strip()
        actual_hash = (local_state.get("hash") or "").strip()
        phase = (snapshot.get("state") or "idle").strip() or "idle"
        origin_participant = (snapshot.get("originParticipant") or "").strip()

        updates = {
            "scope": self.orchestration_sync_scope,
            "actualHash": actual_hash,
            "actualDatabaseCount": int(local_state.get("databaseCount") or 0),
            "actualTableCount": int(local_state.get("tableCount") or 0),
            "actualRowCount": int(local_state.get("rowCount") or 0),
            "authFileCount": int(local_state.get("authFileCount") or 0),
            "sequenceStride": self.orchestration_sync_sequence_stride,
            "sequenceResidue": self.orchestration_sync_sequence_residue,
            "sequenceCount": int(local_state.get("sequenceCount") or 0),
        }
        if desired_hash and desired_hash == actual_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        elif desired_hash and origin_participant and origin_participant != self.pod_name and phase in {
            "pending",
            "in-progress",
            "failed",
        }:
            updates["state"] = phase
        else:
            updates.update(
                {
                    "state": "observed",
                    "desiredHash": actual_hash,
                    "desiredDatabaseCount": int(local_state.get("databaseCount") or 0),
                    "desiredTableCount": int(local_state.get("tableCount") or 0),
                    "desiredRowCount": int(local_state.get("rowCount") or 0),
                    "originParticipant": self.pod_name,
                    "desiredPublishedAt": observed_at,
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        self.orchestration_sync_runtime_error = ""
        self.merge_orchestration_sync_state(**updates)

    def build_orchestration_state_payload(self, state, published_at):
        payload_state = {
            "scope": state.get("scope", self.orchestration_sync_scope),
            "hash": state.get("hash", ""),
            "databaseCount": int(state.get("databaseCount") or 0),
            "tableCount": int(state.get("tableCount") or 0),
            "rowCount": int(state.get("rowCount") or 0),
            "authFileCount": int(state.get("authFileCount") or 0),
            "sequenceCount": int(state.get("sequenceCount") or 0),
        }
        return {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorRelayOrchestrationState",
            "publishedAt": published_at,
            "origin": {
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "role": self.relay_role,
                "segment": self.segment_name,
            },
            "targetRoles": list(self.orchestration_sync_target_roles),
            "state": payload_state,
        }

    def record_published_orchestration_state(self, state, published_at, now):
        state_hash = (state.get("hash") or "").strip()
        self.merge_orchestration_sync_state(
            state="converged",
            scope=self.orchestration_sync_scope,
            desiredHash=state_hash,
            actualHash=state_hash,
            desiredDatabaseCount=int(state.get("databaseCount") or 0),
            actualDatabaseCount=int(state.get("databaseCount") or 0),
            desiredTableCount=int(state.get("tableCount") or 0),
            actualTableCount=int(state.get("tableCount") or 0),
            desiredRowCount=int(state.get("rowCount") or 0),
            actualRowCount=int(state.get("rowCount") or 0),
            authFileCount=int(state.get("authFileCount") or 0),
            sequenceStride=self.orchestration_sync_sequence_stride,
            sequenceResidue=self.orchestration_sync_sequence_residue,
            sequenceCount=int(state.get("sequenceCount") or 0),
            originParticipant=self.pod_name,
            desiredPublishedAt=published_at,
            lastPublishedAt=now,
            lastConvergedAt=now,
            lastError="",
        )

    def publish_orchestration_state(self):
        if (
            not self.orchestration_sync_enabled
            or self.connection is None
            or self.channel is None
            or self.bundle is None
        ):
            return

        state = self.read_local_orchestration_state()
        now = int(time.time())
        state_hash = (state.get("hash") or "").strip()
        should_publish = (
            state_hash != self.last_published_orchestration_hash
            or now >= self.next_orchestration_publish
        )
        if not should_publish:
            return

        snapshot = self.orchestration_sync_state_snapshot()
        version_at = max(int(snapshot.get("desiredPublishedAt") or 0), now)
        if (snapshot.get("desiredHash") or "").strip() != state_hash:
            version_at = now

        self.upsert_shared_orchestration_state(state, version_at)

        payload = self.build_orchestration_state_payload(state, version_at)
        self.channel.basic_publish(
            exchange=self.bundle["hub"]["exchanges"]["data"],
            routing_key=f"relay.orchestration-state.{sanitize_fragment(self.pod_name)}",
            body=stable_json(payload).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )
        self.last_published_orchestration_hash = state_hash
        self.next_orchestration_publish = now + self.publish_interval
        self.record_published_orchestration_state(state, version_at, now)

    def apply_orchestration_auth_files(self, auth_files):
        changed = False
        target_paths = self.orchestration_sync_auth_file_paths()
        for logical_name, encoded in (auth_files or {}).items():
            target_path = target_paths.get(logical_name)
            if not target_path:
                continue
            content = base64.b64decode(encoded.encode("ascii"))
            current_content = b""
            if os.path.isfile(target_path):
                with open(target_path, "rb") as handle:
                    current_content = handle.read()
            if current_content == content:
                continue
            self.write_managed_file(
                target_path,
                content,
                target_path,
                0o640,
            )
            changed = True
        return changed

    def replace_orchestration_table(self, cursor, database_name, table_name, rows):
        table_spec = self.orchestration_sync_db_spec(database_name)["tables"].get(table_name)
        if table_spec is None:
            raise RuntimeError(
                f"unsupported orchestration sync table {database_name}.{table_name}"
            )
        payload = json.dumps(rows, separators=(",", ":"), sort_keys=True)
        table_identifier = sql_identifier(table_name)
        column_names = [name for name, _type_name in table_spec.get("columns") or []]
        column_list = ", ".join(sql_identifier(name) for name in column_names)
        recordset_columns = ",\n                        ".join(
            f"{sql_identifier(name)} {type_name}"
            for name, type_name in table_spec.get("columns") or []
        )
        cursor.execute(f"delete from {table_identifier}")
        if not rows:
            return
        cursor.execute(
            f"""
                    insert into {table_identifier} ({column_list})
                    select {column_list}
                    from json_to_recordset(%s::json) as x(
                        {recordset_columns}
                    )
                    """,
            (payload,),
        )

    def reconcile_orchestration_database(self, database_name, conf_path, desired_tables):
        spec = self.orchestration_sync_db_spec(database_name)
        self.prepare_orchestration_database_sequences(database_name, conf_path)
        connection = self.orchestration_db_connection(conf_path)
        try:
            cursor = connection.cursor()
            for table_name in spec.get("clearOrder") or []:
                self.replace_orchestration_table(cursor, database_name, table_name, [])
            for table_name in spec.get("loadOrder") or []:
                self.replace_orchestration_table(
                    cursor,
                    database_name,
                    table_name,
                    desired_tables.get(table_name) or [],
                )
            connection.commit()
            cursor.close()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reconcile_orchestration_state(self, state_payload, published_at, origin_participant):
        shared = self.wait_for_shared_orchestration_state(published_at)
        if shared is None:
            raise RuntimeError("shared orchestration sync state is missing from Cassandra")
        shared_published_at = int(shared.get("publishedAt") or 0)
        state_payload = dict(shared.get("state") or {})
        origin_participant = (shared.get("originParticipant") or "").strip() or origin_participant
        published_at = shared_published_at or int(published_at or time.time())

        desired_databases = state_payload.get("databases") or {}
        if not isinstance(desired_databases, dict):
            raise RuntimeError("orchestration sync payload databases are invalid")

        auth_files = dict(state_payload.get("authFiles") or {})
        self.apply_orchestration_auth_files(auth_files)

        for database_name in self.orchestration_sync_database_names():
            database_payload = desired_databases.get(database_name) or {}
            if not isinstance(database_payload, dict):
                raise RuntimeError(
                    f"orchestration sync payload database {database_name} is invalid"
                )
            desired_tables_payload = database_payload.get("tables") or {}
            if not isinstance(desired_tables_payload, dict):
                raise RuntimeError(
                    f"orchestration sync payload tables for {database_name} are invalid"
                )
            desired_tables = {}
            for table_name in self.orchestration_sync_db_spec(database_name)["tables"]:
                rows = desired_tables_payload.get(table_name) or []
                if not isinstance(rows, list):
                    raise RuntimeError(
                        f"orchestration sync payload table {database_name}.{table_name} is not a list"
                    )
                desired_tables[table_name] = rows
            self.reconcile_orchestration_database(
                database_name,
                self.orchestration_db_conf_path(database_name),
                desired_tables,
            )

        local_state = self.read_local_orchestration_state()
        actual_hash = (local_state.get("hash") or "").strip()
        desired_hash = (state_payload.get("hash") or "").strip()
        updates = {
            "scope": state_payload.get("scope") or self.orchestration_sync_scope,
            "desiredHash": desired_hash,
            "actualHash": actual_hash,
            "desiredDatabaseCount": int(
                state_payload.get("databaseCount") or len(self.orchestration_sync_database_names())
            ),
            "actualDatabaseCount": int(local_state.get("databaseCount") or 0),
            "desiredTableCount": int(state_payload.get("tableCount") or 0),
            "actualTableCount": int(local_state.get("tableCount") or 0),
            "desiredRowCount": int(state_payload.get("rowCount") or 0),
            "actualRowCount": int(local_state.get("rowCount") or 0),
            "authFileCount": int(local_state.get("authFileCount") or 0),
            "sequenceStride": self.orchestration_sync_sequence_stride,
            "sequenceResidue": self.orchestration_sync_sequence_residue,
            "sequenceCount": int(local_state.get("sequenceCount") or 0),
            "originParticipant": origin_participant,
            "desiredPublishedAt": int(published_at or time.time()),
            "lastAppliedAt": int(time.time()),
        }
        if actual_hash == desired_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": int(time.time()),
                    "lastError": "",
                }
            )
        else:
            updates.update(
                {
                    "state": "failed",
                    "lastError": (
                        "orchestration managed-domain hash mismatch after apply: "
                        f"expected {desired_hash}, got {actual_hash or 'none'}"
                    ),
                }
            )
        self.orchestration_sync_runtime_error = ""
        self.merge_orchestration_sync_state(**updates)

    def ensure_orchestration_sync_cassandra_session(self):
        if not self.orchestration_sync_enabled:
            raise RuntimeError("orchestration sync is not enabled")
        if not self.orchestration_sync_cassandra_contact_points:
            raise RuntimeError("no Cassandra contact points configured for orchestration sync")
        if Cluster is None or ConsistencyLevel is None or SimpleStatement is None:
            raise RuntimeError("cassandra-driver is not available in the Conductor image")

        with self.lock:
            if self.orchestration_sync_cassandra_session is not None:
                return self.orchestration_sync_cassandra_session

            cluster = Cluster(
                contact_points=self.orchestration_sync_cassandra_contact_points,
                port=self.orchestration_sync_cassandra_port,
            )
            session = cluster.connect()
            keyspace = self.orchestration_sync_cassandra_keyspace
            table = self.orchestration_sync_cassandra_table
            replication_factor = max(1, self.orchestration_sync_cassandra_replication_factor)
            session.execute(
                f"""
                create keyspace if not exists {keyspace}
                with replication = {{
                    'class': 'SimpleStrategy',
                    'replication_factor': {replication_factor}
                }}
                """
            )
            session.set_keyspace(keyspace)
            session.execute(
                f"""
                create table if not exists {table} (
                    scope text primary key,
                    published_at bigint,
                    origin_participant text,
                    state_hash text,
                    payload text
                )
                """
            )
            self.orchestration_sync_cassandra_cluster = cluster
            self.orchestration_sync_cassandra_session = session
            return session

    def read_shared_orchestration_state(self):
        session = self.ensure_orchestration_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"select scope, published_at, origin_participant, state_hash, payload "
                f"from {self.orchestration_sync_cassandra_table} where scope = %s"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        row = session.execute(statement, (self.orchestration_sync_scope,)).one()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload or "{}")
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"invalid shared orchestration sync payload: {error}") from error
        payload_hash = ((payload or {}).get("hash") or "").strip()
        expected_hash = (row.state_hash or "").strip()
        if expected_hash and payload_hash and payload_hash != expected_hash:
            raise RuntimeError(
                "shared orchestration sync payload hash mismatch: "
                f"expected {expected_hash}, got {payload_hash}"
            )
        return {
            "scope": (row.scope or "").strip(),
            "publishedAt": int(row.published_at or 0),
            "originParticipant": (row.origin_participant or "").strip(),
            "stateHash": expected_hash or payload_hash,
            "state": payload if isinstance(payload, dict) else {},
        }

    def wait_for_shared_orchestration_state(self, received_published_at=0, timeout_seconds=15):
        deadline = time.time() + max(1, int(timeout_seconds or 0))
        while True:
            shared = self.read_shared_orchestration_state()
            if shared is None:
                if time.time() >= deadline:
                    return None
            else:
                shared_published_at = int(shared.get("publishedAt") or 0)
                if not received_published_at or shared_published_at >= received_published_at:
                    return shared
                if time.time() >= deadline:
                    raise RuntimeError(
                        "shared orchestration sync state is older than the received intent: "
                        f"{shared_published_at} < {received_published_at}"
                    )
            time.sleep(1)

    def upsert_shared_orchestration_state(self, state, published_at):
        state_hash = ((state or {}).get("hash") or "").strip()
        if not state_hash:
            raise RuntimeError("orchestration sync state is missing a hash")
        session = self.ensure_orchestration_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"insert into {self.orchestration_sync_cassandra_table} "
                "(scope, published_at, origin_participant, state_hash, payload) "
                "values (%s, %s, %s, %s, %s)"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        session.execute(
            statement,
            (
                self.orchestration_sync_scope,
                int(published_at or time.time()),
                self.pod_name,
                state_hash,
                stable_json(state),
            ),
        )

    def handle_remote_orchestration_state(self, payload):
        if not self.orchestration_sync_enabled:
            return

        origin = payload.get("origin") or {}
        origin_participant = (origin.get("participant") or "").strip()
        if not origin_participant or origin_participant == self.pod_name:
            return

        target_roles = payload.get("targetRoles") or []
        if target_roles and self.relay_role not in target_roles:
            return

        state_payload = payload.get("state") or {}
        desired_hash = (state_payload.get("hash") or "").strip()
        if not desired_hash:
            raise RuntimeError("orchestration sync payload is missing a hash")

        published_at = int(payload.get("publishedAt") or 0)
        shared_state = self.wait_for_shared_orchestration_state(published_at)
        if shared_state is None:
            raise RuntimeError("shared orchestration sync state is missing from Cassandra")
        shared_hash = (shared_state.get("stateHash") or "").strip()
        shared_published_at = int(shared_state.get("publishedAt") or 0)
        if shared_hash:
            desired_hash = shared_hash
        published_at = shared_published_at or published_at
        origin_participant = (
            (shared_state.get("originParticipant") or "").strip() or origin_participant
        )
        shared_payload = shared_state.get("state") or {}
        if isinstance(shared_payload, dict):
            state_payload = shared_payload
        current_state = self.orchestration_sync_state_snapshot()
        current_desired_hash = (current_state.get("desiredHash") or "").strip()
        current_actual_hash = (current_state.get("actualHash") or "").strip()
        current_published_at = int(current_state.get("desiredPublishedAt") or 0)
        if current_published_at and published_at and published_at < current_published_at:
            return
        if (
            current_published_at
            and published_at
            and published_at == current_published_at
            and desired_hash == current_desired_hash
        ):
            return
        if desired_hash == current_actual_hash and desired_hash == current_desired_hash:
            self.merge_orchestration_sync_state(
                state="converged",
                lastReceivedAt=int(time.time()),
                lastConvergedAt=int(time.time()),
                lastError="",
            )
            return

        self.merge_orchestration_sync_state(
            state="pending",
            scope=state_payload.get("scope") or self.orchestration_sync_scope,
            desiredHash=desired_hash,
            desiredDatabaseCount=int(
                state_payload.get("databaseCount") or len(self.orchestration_sync_database_names())
            ),
            desiredTableCount=int(state_payload.get("tableCount") or 0),
            desiredRowCount=int(state_payload.get("rowCount") or 0),
            authFileCount=int(state_payload.get("authFileCount") or 0),
            sequenceStride=self.orchestration_sync_sequence_stride,
            sequenceResidue=self.orchestration_sync_sequence_residue,
            sequenceCount=int(state_payload.get("sequenceCount") or 0),
            originParticipant=origin_participant,
            desiredPublishedAt=published_at or int(time.time()),
            lastReceivedAt=int(time.time()),
            lastError="",
        )
        log(
            "Received orchestration sync intent at "
            f"{desired_hash[:12]} from {origin_participant}"
        )
        self.reconcile_orchestration_state(state_payload, published_at, origin_participant)

    def ensure_inventory_sync_cassandra_session(self):
        if not self.inventory_sync_enabled:
            raise RuntimeError("inventory sync is not enabled")
        if not self.inventory_sync_cassandra_contact_points:
            raise RuntimeError("no Cassandra contact points configured for inventory sync")
        if Cluster is None or ConsistencyLevel is None or SimpleStatement is None:
            raise RuntimeError("cassandra-driver is not available in the Conductor image")

        with self.lock:
            if self.inventory_sync_cassandra_session is not None:
                return self.inventory_sync_cassandra_session

            cluster = Cluster(
                contact_points=self.inventory_sync_cassandra_contact_points,
                port=self.inventory_sync_cassandra_port,
            )
            session = cluster.connect()
            keyspace = self.inventory_sync_cassandra_keyspace
            table = self.inventory_sync_cassandra_table
            replication_factor = max(1, self.inventory_sync_cassandra_replication_factor)
            session.execute(
                f"""
                create keyspace if not exists {keyspace}
                with replication = {{
                    'class': 'SimpleStrategy',
                    'replication_factor': {replication_factor}
                }}
                """
            )
            session.set_keyspace(keyspace)
            session.execute(
                f"""
                create table if not exists {table} (
                    scope text primary key,
                    published_at bigint,
                    origin_participant text,
                    state_hash text,
                    payload text
                )
                """
            )
            self.inventory_sync_cassandra_cluster = cluster
            self.inventory_sync_cassandra_session = session
            return session

    def read_shared_inventory_state(self):
        session = self.ensure_inventory_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"select scope, published_at, origin_participant, state_hash, payload "
                f"from {self.inventory_sync_cassandra_table} where scope = %s"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        row = session.execute(statement, (self.inventory_sync_scope,)).one()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload or "{}")
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"invalid shared inventory sync payload: {error}") from error
        payload_hash = ((payload or {}).get("hash") or "").strip()
        expected_hash = (row.state_hash or "").strip()
        if expected_hash and payload_hash and payload_hash != expected_hash:
            raise RuntimeError(
                "shared inventory sync payload hash mismatch: "
                f"expected {expected_hash}, got {payload_hash}"
            )
        return {
            "scope": (row.scope or "").strip(),
            "publishedAt": int(row.published_at or 0),
            "originParticipant": (row.origin_participant or "").strip(),
            "stateHash": expected_hash or payload_hash,
            "state": payload if isinstance(payload, dict) else {},
        }

    def wait_for_shared_inventory_state(self, received_published_at=0, timeout_seconds=15):
        deadline = time.time() + max(1, int(timeout_seconds or 0))
        while True:
            shared = self.read_shared_inventory_state()
            if shared is None:
                if time.time() >= deadline:
                    return None
            else:
                shared_published_at = int(shared.get("publishedAt") or 0)
                if not received_published_at or shared_published_at >= received_published_at:
                    return shared
                if time.time() >= deadline:
                    raise RuntimeError(
                        "shared inventory sync state is older than the received intent: "
                        f"{shared_published_at} < {received_published_at}"
                    )
            time.sleep(1)

    def upsert_shared_inventory_state(self, state, published_at):
        state_hash = ((state or {}).get("hash") or "").strip()
        if not state_hash:
            raise RuntimeError("inventory sync state is missing a hash")
        session = self.ensure_inventory_sync_cassandra_session()
        statement = SimpleStatement(
            (
                f"insert into {self.inventory_sync_cassandra_table} "
                "(scope, published_at, origin_participant, state_hash, payload) "
                "values (%s, %s, %s, %s, %s)"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        session.execute(
            statement,
            (
                self.inventory_sync_scope,
                int(published_at or time.time()),
                self.pod_name,
                state_hash,
                stable_json(state),
            ),
        )

    def read_local_inventory_sync_state(self):
        full_database = self.read_orchestration_database_state(
            "inventory",
            self.inventory_sync_inventory_conf_path,
        )
        database = self.inventory_shared_database_state(full_database)
        auth_files = {}
        for logical_name, source_path in self.inventory_sync_auth_file_paths().items():
            if not source_path or not os.path.isfile(source_path):
                raise RuntimeError(
                    f"inventory auth file is missing: {source_path or logical_name}"
                )
            with open(source_path, "rb") as handle:
                auth_files[logical_name] = base64.b64encode(handle.read()).decode("ascii")
        payload = {
            "scope": self.inventory_sync_scope,
            "database": database,
            "tableCount": int(database.get("tableCount") or 0),
            "rowCount": int(database.get("rowCount") or 0),
            "authFiles": auth_files,
            "authFileCount": len(auth_files),
        }
        payload["hash"] = self.orchestration_rows_hash({"inventory": database}, auth_files)
        return payload

    @staticmethod
    def inventory_row_id_set(rows, column_name="id"):
        identifiers = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            value = row.get(column_name)
            if value is None:
                continue
            identifiers.add(str(value))
        return identifiers

    @staticmethod
    def inventory_is_local_only_connection(row):
        if not isinstance(row, dict):
            return False
        return bool(row.get("undiscoverable"))

    def inventory_split_database_state(self, database_state):
        table_names = list((database_state or {}).get("tableNames") or [])
        tables = {
            table_name: list(((database_state or {}).get("tables") or {}).get(table_name) or [])
            for table_name in table_names
        }

        shared_connections = []
        local_connections = []
        for row in tables.get("connections") or []:
            if self.inventory_is_local_only_connection(row):
                local_connections.append(row)
            else:
                shared_connections.append(row)

        shared_connection_ids = self.inventory_row_id_set(shared_connections)
        local_connection_ids = self.inventory_row_id_set(local_connections)

        shared_parameter_ids = self.inventory_row_id_set(shared_connections, "parameters")
        shared_sensitive_ids = self.inventory_row_id_set(shared_connections, "sensitive_parameters")
        local_parameter_ids = self.inventory_row_id_set(local_connections, "parameters")
        local_sensitive_ids = self.inventory_row_id_set(local_connections, "sensitive_parameters")

        shared_tables = {
            "connections": shared_connections,
            "parameters": [
                row
                for row in tables.get("parameters") or []
                if str((row or {}).get("id")) in shared_parameter_ids
            ],
            "sensitive_parameters": [
                row
                for row in tables.get("sensitive_parameters") or []
                if str((row or {}).get("id")) in shared_sensitive_ids
            ],
            "connection_metadata": [
                row
                for row in tables.get("connection_metadata") or []
                if str((row or {}).get("connection_id")) in shared_connection_ids
            ],
        }
        local_tables = {
            "connections": local_connections,
            "parameters": [
                row
                for row in tables.get("parameters") or []
                if str((row or {}).get("id")) in local_parameter_ids
            ],
            "sensitive_parameters": [
                row
                for row in tables.get("sensitive_parameters") or []
                if str((row or {}).get("id")) in local_sensitive_ids
            ],
            "connection_metadata": [
                row
                for row in tables.get("connection_metadata") or []
                if str((row or {}).get("connection_id")) in local_connection_ids
            ],
        }

        def build_state(filtered_tables):
            return {
                "tableNames": table_names,
                "tables": {
                    table_name: list(filtered_tables.get(table_name) or [])
                    for table_name in table_names
                },
                "tableCount": len(table_names),
                "rowCount": sum(
                    len(filtered_tables.get(table_name) or []) for table_name in table_names
                ),
                "sequenceCount": int((database_state or {}).get("sequenceCount") or 0),
            }

        return {
            "shared": build_state(shared_tables),
            "local": build_state(local_tables),
        }

    def inventory_shared_database_state(self, database_state):
        split = self.inventory_split_database_state(database_state)
        return split["shared"]

    def merge_inventory_database_state(self, local_database_state, shared_database_state):
        local_tables = self.inventory_split_database_state(local_database_state)["local"]["tables"]
        shared_tables = dict((shared_database_state or {}).get("tables") or {})
        table_names = list(
            (shared_database_state or {}).get("tableNames")
            or (local_database_state or {}).get("tableNames")
            or []
        )
        merged_tables = {}
        for table_name in table_names:
            merged_tables[table_name] = list(local_tables.get(table_name) or []) + list(
                shared_tables.get(table_name) or []
            )
        return merged_tables

    def refresh_local_inventory_sync_state(self):
        if not self.inventory_sync_enabled:
            return

        observed_at = int(time.time())
        local_state = self.read_local_inventory_sync_state()
        snapshot = self.inventory_sync_state_snapshot()
        desired_hash = (snapshot.get("desiredHash") or "").strip()
        actual_hash = (local_state.get("hash") or "").strip()
        phase = (snapshot.get("state") or "idle").strip() or "idle"
        origin_participant = (snapshot.get("originParticipant") or "").strip()

        updates = {
            "scope": self.inventory_sync_scope,
            "actualHash": actual_hash,
            "actualTableCount": int(local_state.get("tableCount") or 0),
            "actualRowCount": int(local_state.get("rowCount") or 0),
            "authFileCount": int(local_state.get("authFileCount") or 0),
        }
        if desired_hash and desired_hash == actual_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        elif desired_hash and origin_participant and origin_participant != self.pod_name and phase in {
            "pending",
            "in-progress",
            "failed",
        }:
            updates["state"] = phase
        else:
            updates.update(
                {
                    "state": "observed",
                    "desiredHash": actual_hash,
                    "desiredTableCount": int(local_state.get("tableCount") or 0),
                    "desiredRowCount": int(local_state.get("rowCount") or 0),
                    "originParticipant": self.pod_name,
                    "desiredPublishedAt": observed_at,
                    "lastConvergedAt": observed_at,
                    "lastError": "",
                }
            )
        self.inventory_sync_runtime_error = ""
        self.merge_inventory_sync_state(**updates)

    def build_inventory_state_payload(self, state, published_at):
        return {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorRelayInventoryState",
            "publishedAt": published_at,
            "origin": {
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "role": self.relay_role,
                "segment": self.segment_name,
            },
            "targetRoles": list(self.inventory_sync_target_roles),
            "state": {
                "scope": state.get("scope", self.inventory_sync_scope),
                "hash": state.get("hash", ""),
                "tableCount": int(state.get("tableCount") or 0),
                "rowCount": int(state.get("rowCount") or 0),
                "authFileCount": int(state.get("authFileCount") or 0),
            },
        }

    def record_published_inventory_state(self, state, published_at, now):
        state_hash = (state.get("hash") or "").strip()
        self.merge_inventory_sync_state(
            state="converged",
            scope=self.inventory_sync_scope,
            desiredHash=state_hash,
            actualHash=state_hash,
            desiredTableCount=int(state.get("tableCount") or 0),
            actualTableCount=int(state.get("tableCount") or 0),
            desiredRowCount=int(state.get("rowCount") or 0),
            actualRowCount=int(state.get("rowCount") or 0),
            authFileCount=int(state.get("authFileCount") or 0),
            originParticipant=self.pod_name,
            desiredPublishedAt=published_at,
            lastPublishedAt=now,
            lastConvergedAt=now,
            lastError="",
        )

    def publish_inventory_state(self):
        if (
            not self.inventory_sync_enabled
            or self.connection is None
            or self.channel is None
            or self.bundle is None
        ):
            return

        state = self.read_local_inventory_sync_state()
        now = int(time.time())
        state_hash = (state.get("hash") or "").strip()
        should_publish = (
            state_hash != self.last_published_inventory_hash
            or now >= self.next_inventory_publish
        )
        if not should_publish:
            return

        snapshot = self.inventory_sync_state_snapshot()
        version_at = max(int(snapshot.get("desiredPublishedAt") or 0), now)
        if (snapshot.get("desiredHash") or "").strip() != state_hash:
            version_at = now

        self.upsert_shared_inventory_state(state, version_at)
        payload = self.build_inventory_state_payload(state, version_at)
        self.channel.basic_publish(
            exchange=self.bundle["hub"]["exchanges"]["data"],
            routing_key=f"relay.inventory-state.{sanitize_fragment(self.pod_name)}",
            body=stable_json(payload).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
        )
        self.last_published_inventory_hash = state_hash
        self.next_inventory_publish = now + self.publish_interval
        self.record_published_inventory_state(state, version_at, now)

    def reconcile_inventory_state(self, state_payload, published_at, origin_participant):
        shared = self.wait_for_shared_inventory_state(published_at)
        if shared is None:
            raise RuntimeError("shared inventory sync state is missing from Cassandra")
        shared_published_at = int(shared.get("publishedAt") or 0)

        shared_state = dict(shared.get("state") or {})
        state_payload = shared_state
        origin_participant = (shared.get("originParticipant") or "").strip() or origin_participant
        published_at = shared_published_at or int(published_at or time.time())
        auth_files = dict(shared_state.get("authFiles") or {})
        self.apply_orchestration_auth_files(auth_files)

        database_payload = shared_state.get("database") or {}
        if not isinstance(database_payload, dict):
            raise RuntimeError("inventory sync payload database is invalid")
        desired_tables_payload = database_payload.get("tables") or {}
        if not isinstance(desired_tables_payload, dict):
            raise RuntimeError("inventory sync payload tables are invalid")
        desired_tables = {}
        for table_name in self.orchestration_sync_db_spec("inventory")["tables"]:
            rows = desired_tables_payload.get(table_name) or []
            if not isinstance(rows, list):
                raise RuntimeError(f"inventory sync payload table inventory.{table_name} is not a list")
            desired_tables[table_name] = rows
        local_database = self.read_orchestration_database_state(
            "inventory",
            self.inventory_sync_inventory_conf_path,
        )
        desired_database = {
            "tableNames": list(database_payload.get("tableNames") or []),
            "tables": desired_tables,
            "tableCount": int(database_payload.get("tableCount") or 0),
            "rowCount": int(database_payload.get("rowCount") or 0),
            "sequenceCount": int(database_payload.get("sequenceCount") or 0),
        }
        merged_tables = self.merge_inventory_database_state(local_database, desired_database)
        self.reconcile_orchestration_database(
            "inventory",
            self.inventory_sync_inventory_conf_path,
            merged_tables,
        )

        local_state = self.read_local_inventory_sync_state()
        actual_hash = (local_state.get("hash") or "").strip()
        desired_hash = (state_payload.get("hash") or "").strip()
        updates = {
            "scope": state_payload.get("scope") or self.inventory_sync_scope,
            "desiredHash": desired_hash,
            "actualHash": actual_hash,
            "desiredTableCount": int(state_payload.get("tableCount") or 0),
            "actualTableCount": int(local_state.get("tableCount") or 0),
            "desiredRowCount": int(state_payload.get("rowCount") or 0),
            "actualRowCount": int(local_state.get("rowCount") or 0),
            "authFileCount": int(local_state.get("authFileCount") or 0),
            "originParticipant": origin_participant,
            "desiredPublishedAt": int(published_at or time.time()),
            "lastAppliedAt": int(time.time()),
        }
        if actual_hash == desired_hash:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": int(time.time()),
                    "lastError": "",
                }
            )
        else:
            updates.update(
                {
                    "state": "failed",
                    "lastError": (
                        "inventory managed-domain hash mismatch after apply: "
                        f"expected {desired_hash}, got {actual_hash or 'none'}"
                    ),
                }
            )
        self.inventory_sync_runtime_error = ""
        self.merge_inventory_sync_state(**updates)

    def handle_remote_inventory_state(self, payload):
        if not self.inventory_sync_enabled:
            return

        origin = payload.get("origin") or {}
        origin_participant = (origin.get("participant") or "").strip()
        if not origin_participant or origin_participant == self.pod_name:
            return

        target_roles = payload.get("targetRoles") or []
        if target_roles and self.relay_role not in target_roles:
            return

        state_payload = payload.get("state") or {}
        desired_hash = (state_payload.get("hash") or "").strip()
        if not desired_hash:
            raise RuntimeError("inventory sync payload is missing a hash")

        published_at = int(payload.get("publishedAt") or 0)
        shared = self.wait_for_shared_inventory_state(published_at)
        if shared is None:
            raise RuntimeError("shared inventory sync state is missing from Cassandra")
        shared_hash = (shared.get("stateHash") or "").strip()
        shared_published_at = int(shared.get("publishedAt") or 0)
        if shared_hash:
            desired_hash = shared_hash
        published_at = shared_published_at or published_at
        origin_participant = (shared.get("originParticipant") or "").strip() or origin_participant
        shared_payload = shared.get("state") or {}
        if isinstance(shared_payload, dict):
            state_payload = shared_payload
        current_state = self.inventory_sync_state_snapshot()
        current_desired_hash = (current_state.get("desiredHash") or "").strip()
        current_actual_hash = (current_state.get("actualHash") or "").strip()
        current_published_at = int(current_state.get("desiredPublishedAt") or 0)
        if current_published_at and published_at and published_at < current_published_at:
            return
        if (
            current_published_at
            and published_at
            and published_at == current_published_at
            and desired_hash == current_desired_hash
        ):
            return
        if desired_hash == current_actual_hash and desired_hash == current_desired_hash:
            self.merge_inventory_sync_state(
                state="converged",
                lastReceivedAt=int(time.time()),
                lastConvergedAt=int(time.time()),
                lastError="",
            )
            return

        self.merge_inventory_sync_state(
            state="pending",
            scope=state_payload.get("scope") or self.inventory_sync_scope,
            desiredHash=desired_hash,
            desiredTableCount=int(state_payload.get("tableCount") or 0),
            desiredRowCount=int(state_payload.get("rowCount") or 0),
            authFileCount=int(state_payload.get("authFileCount") or 0),
            originParticipant=origin_participant,
            desiredPublishedAt=published_at or int(time.time()),
            lastReceivedAt=int(time.time()),
            lastError="",
        )
        log(
            "Received inventory sync intent at "
            f"{desired_hash[:12]} from {origin_participant}"
        )
        self.reconcile_inventory_state(state_payload, published_at, origin_participant)

    def code_deploy_hook_url(self):
        return (
            f"https://{self.code_deploy_service_host}:"
            f"{self.code_deploy_hook_listen_port}{self.code_deploy_hook_path}"
        )

    def refresh_code_deploy_summary_locked(self):
        hook_ready = not self.code_deploy_enabled or (
            self.code_deploy_hook_server is not None
            and self.code_deploy_hook_thread is not None
            and self.code_deploy_hook_thread.is_alive()
        )
        states = {
            environment: dict(state)
            for environment, state in sorted(self.code_deploy_states.items())
        }

        pending_count = 0
        failure_count = 0
        converged_count = 0
        last_published_at = 0
        last_converged_at = 0
        ready = not self.code_deploy_enabled or hook_ready

        for state in states.values():
            desired_signature = (state.get("desiredSignature") or "").strip()
            actual_signature = (state.get("actualSignature") or "").strip()
            phase = (state.get("state") or "idle").strip() or "idle"

            if phase == "failed":
                failure_count += 1
                ready = False
            elif desired_signature and actual_signature == desired_signature and phase == "converged":
                converged_count += 1
            elif phase in {"pending", "in-progress"} or (
                desired_signature and actual_signature != desired_signature
            ):
                pending_count += 1
                ready = False

            last_published_at = max(
                last_published_at,
                int(state.get("lastPublishedAt") or 0),
                int(state.get("desiredPublishedAt") or 0),
            )
            last_converged_at = max(last_converged_at, int(state.get("lastConvergedAt") or 0))

        last_error = self.code_deploy_hook_start_error or self.code_deploy_runtime_error or ""
        if not last_error:
            error_candidates = [
                (
                    int(state.get("lastErrorAt") or 0),
                    environment,
                    (state.get("lastError") or "").strip(),
                )
                for environment, state in states.items()
                if (state.get("lastError") or "").strip()
            ]
            if error_candidates:
                last_error = max(error_candidates)[2]

        self.status.update(
            {
                "codeDeployHookReady": hook_ready,
                "codeDeployReady": ready,
                "codeDeployHookUrl": self.code_deploy_hook_url() if self.code_deploy_enabled else "",
                "codeDeployEnvironmentCount": len(states),
                "codeDeployConvergedCount": converged_count,
                "codeDeployPendingCount": pending_count,
                "codeDeployFailureCount": failure_count,
                "codeDeployLastPublishedAt": last_published_at,
                "codeDeployLastConvergedAt": last_converged_at,
                "codeDeployLastError": last_error,
                "codeDeployEnvironments": states,
            }
        )

    def refresh_code_deploy_summary(self):
        with self.lock:
            self.refresh_code_deploy_summary_locked()

    def persist_code_deploy_state(self):
        with self.lock:
            payload = {
                "apiVersion": "pe-k8s.puppet.com/v1alpha1",
                "kind": "ConductorRelayCodeDeployState",
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "environments": {
                    environment: dict(state)
                    for environment, state in sorted(self.code_deploy_states.items())
                },
            }
        write_text_file(self.code_deploy_state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def load_code_deploy_state(self):
        states = {}
        if self.code_deploy_enabled and os.path.isfile(self.code_deploy_state_path):
            try:
                payload = read_json_file(self.code_deploy_state_path)
                raw_states = payload.get("environments") or {}
                for raw_environment, raw_state in raw_states.items():
                    environment = ((raw_state or {}).get("environment") or raw_environment or "").strip()
                    if not environment:
                        continue
                    state = self.default_code_deploy_state(environment)
                    if isinstance(raw_state, dict):
                        state.update(raw_state)
                    state["environment"] = environment
                    states[environment] = state
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                log(f"Failed to load code deploy state: {error}")
        with self.lock:
            self.code_deploy_states = states
            self.refresh_code_deploy_summary_locked()

    def code_deploy_state_for(self, environment):
        environment = (environment or "").strip()
        if not environment:
            return self.default_code_deploy_state("")
        with self.lock:
            state = dict(
                self.code_deploy_states.get(environment) or self.default_code_deploy_state(environment)
            )
        state["environment"] = environment
        return state

    def merge_code_deploy_state(self, environment, **updates):
        environment = (environment or "").strip()
        if not environment:
            raise RuntimeError("code deploy environment is required")

        with self.lock:
            state = dict(
                self.code_deploy_states.get(environment) or self.default_code_deploy_state(environment)
            )
            state["environment"] = environment
            state.update(updates)
            if "lastError" in updates:
                if (updates.get("lastError") or "").strip():
                    state["lastErrorAt"] = int(updates.get("lastErrorAt") or time.time())
                else:
                    state["lastErrorAt"] = 0
                    self.code_deploy_runtime_error = ""
            self.code_deploy_states[environment] = state
            self.refresh_code_deploy_summary_locked()
            snapshot = dict(state)

        self.persist_code_deploy_state()
        return snapshot

    def suppress_code_deploy_hook(self, environment, expected_signature=""):
        environment = (environment or "").strip()
        if not environment:
            return
        with self.lock:
            self.code_deploy_suppressions[environment] = {
                "expectedSignature": (expected_signature or "").strip(),
                "mode": "any",
                "expiresAt": int(time.time()) + max(self.code_deploy_request_timeout_seconds + 120, 600),
            }

    def narrow_code_deploy_suppression(self, environment, expected_signature=""):
        environment = (environment or "").strip()
        if not environment:
            return
        with self.lock:
            suppression = self.code_deploy_suppressions.get(environment)
            if suppression is None:
                return
            suppression["expectedSignature"] = (expected_signature or "").strip()
            suppression["mode"] = "signature"

    def clear_code_deploy_suppression(self, environment):
        environment = (environment or "").strip()
        if not environment:
            return
        with self.lock:
            self.code_deploy_suppressions.pop(environment, None)

    def hook_is_suppressed(self, environment, signature=""):
        environment = (environment or "").strip()
        if not environment:
            return False

        signature = (signature or "").strip()
        with self.lock:
            suppression = self.code_deploy_suppressions.get(environment)
            if suppression is None:
                return False
            if int(suppression.get("expiresAt") or 0) <= int(time.time()):
                self.code_deploy_suppressions.pop(environment, None)
                return False
            if suppression.get("mode") == "any":
                return True
            expected_signature = (suppression.get("expectedSignature") or "").strip()
            return not expected_signature or expected_signature == signature

    def start_code_deploy_worker(self):
        if not self.code_deploy_enabled:
            return

        if self.code_deploy_worker_thread is not None and self.code_deploy_worker_thread.is_alive():
            return

        self.code_deploy_worker_thread = threading.Thread(
            target=self.code_deploy_worker_loop,
            name="conductor-relay-code-deploy",
            daemon=True,
        )
        self.code_deploy_worker_thread.start()

    def schedule_code_deploy(self, environment):
        environment = (environment or "").strip()
        if not environment:
            return
        with self.lock:
            if environment in self.code_deploy_queued:
                return
            self.code_deploy_queued.add(environment)
        self.code_deploy_queue.put(environment)

    def code_deploy_worker_loop(self):
        while True:
            environment = self.code_deploy_queue.get()
            should_retry = False
            try:
                self.reconcile_code_deploy_environment(environment)
            except Exception as error:  # pragma: no cover - defensive fallback
                log(f"Unexpected code deploy worker failure for {environment}: {error}")
                self.merge_code_deploy_state(
                    environment,
                    state="failed",
                    lastError=str(error),
                )
            finally:
                with self.lock:
                    self.code_deploy_queued.discard(environment)
                    state = dict(self.code_deploy_states.get(environment) or {})
                desired_signature = (state.get("desiredSignature") or "").strip()
                actual_signature = (state.get("actualSignature") or "").strip()
                phase = (state.get("state") or "").strip()
                should_retry = bool(
                    desired_signature
                    and desired_signature != actual_signature
                    and phase not in {"failed", "converged"}
                )
                self.code_deploy_queue.task_done()

            if should_retry:
                self.schedule_code_deploy(environment)

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

    @staticmethod
    def extract_code_deploy_result(payload, environment=""):
        if not isinstance(payload, dict):
            return {
                "environment": (environment or "").strip(),
                "status": "",
                "deployId": 0,
                "deploySignature": "",
                "environmentCommit": "",
                "codeCommit": "",
            }

        file_sync = payload.get("file-sync") or payload.get("fileSync") or {}
        result_environment = ((payload.get("environment") or environment or "")).strip()
        return {
            "environment": result_environment,
            "status": (payload.get("status") or "").strip(),
            "deployId": int(payload.get("id") or 0),
            "deploySignature": (
                payload.get("deploy-signature")
                or payload.get("deploySignature")
                or file_sync.get("deploy-signature")
                or file_sync.get("deploySignature")
                or ""
            ).strip(),
            "environmentCommit": (
                file_sync.get("environment-commit")
                or file_sync.get("environmentCommit")
                or payload.get("environment-commit")
                or payload.get("environmentCommit")
                or ""
            ).strip(),
            "codeCommit": (
                file_sync.get("code-commit")
                or file_sync.get("codeCommit")
                or payload.get("code-commit")
                or payload.get("codeCommit")
                or ""
            ).strip(),
        }

    @classmethod
    def extract_code_manager_status_entries(cls, payload):
        results = {}
        if not isinstance(payload, dict):
            return results

        deployed = ((payload.get("file-sync-storage-status") or {}).get("deployed"))
        if isinstance(deployed, list):
            for item in deployed:
                result = cls.extract_code_deploy_result(item)
                environment = (result.get("environment") or "").strip()
                if environment:
                    results[environment] = result
            return results

        if isinstance(deployed, dict):
            if (deployed.get("environment") or "").strip() or (deployed.get("deploy-signature") or "").strip():
                result = cls.extract_code_deploy_result(deployed)
                environment = (result.get("environment") or "").strip()
                if environment:
                    results[environment] = result
                return results

            for raw_environment, item in deployed.items():
                if isinstance(item, dict):
                    result = cls.extract_code_deploy_result(item, environment=raw_environment)
                else:
                    result = cls.extract_code_deploy_result(
                        {
                            "environment": raw_environment,
                            "deploy-signature": item,
                        }
                    )
                environment = (result.get("environment") or "").strip()
                if environment:
                    results[environment] = result

        return results

    def build_local_service_context(self):
        _, _, _, ca_path = self.puppet_ssl_paths()
        return ssl.create_default_context(cafile=ca_path)

    def console_webserver_conf_text(self):
        if not os.path.isfile(self.console_webserver_conf_path):
            raise RuntimeError(
                f"console webserver config not found: {self.console_webserver_conf_path}"
            )
        with open(self.console_webserver_conf_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def console_webserver_hocon_string(self, key):
        content = self.console_webserver_conf_text()
        match = re.search(
            rf'^\s*{re.escape(key)}\s*:\s*"([^"]+)"',
            content,
            re.MULTILINE,
        )
        if not match:
            raise RuntimeError(
                f"unable to locate {key} in {self.console_webserver_conf_path}"
            )
        return match.group(1)

    def auth_barrier_tls_paths(self):
        cert_path = self.console_webserver_hocon_string("ssl-cert")
        key_path = self.console_webserver_hocon_string("ssl-key")
        ca_path = self.console_webserver_hocon_string("ssl-ca-cert")
        for path in [cert_path, key_path, ca_path]:
            if not os.path.isfile(path):
                raise RuntimeError(f"required auth barrier TLS file not found: {path}")
        return cert_path, key_path, ca_path

    def build_auth_barrier_server_context(self):
        cert_path, key_path, ca_path = self.auth_barrier_tls_paths()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        context.load_verify_locations(cafile=ca_path)
        context.verify_mode = ssl.CERT_OPTIONAL
        return context

    def auth_barrier_peer_host(self, participant_name):
        participant_name = (participant_name or "").strip()
        if not participant_name:
            raise RuntimeError("peer participant name is required")
        return (
            f"{participant_name}.{self.auth_barrier_control_plane_headless_service}."
            f"{self.pod_namespace}.svc.cluster.local"
        )

    def fresh_peer_statuses(self):
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
        return fresh_statuses

    @staticmethod
    def auth_barrier_peer_pod_ready(pod):
        status = (pod or {}).get("status") or {}
        for condition in status.get("conditions") or []:
            if condition.get("type") == "Ready":
                return condition.get("status") == "True"
        return False

    def peer_frontdoor_eligible(self, participant_name):
        try:
            pod = self.k8s.get_pod(participant_name)
        except Exception:
            return False
        if not pod:
            return False

        metadata = pod.get("metadata") or {}
        annotations = metadata.get("annotations") or {}
        if metadata.get("deletionTimestamp"):
            return False
        if not self.auth_barrier_peer_pod_ready(pod):
            return False
        if annotations.get(FRONTDOOR_ELIGIBLE_ANNOTATION) != "true":
            return False

        updated_at = int(annotations.get(FRONTDOOR_UPDATED_AT_ANNOTATION) or 0)
        if updated_at <= 0:
            return False
        if int(time.time()) - updated_at > self.peer_status_max_age:
            return False
        return True

    def auth_barrier_target_peers(self):
        peers = []
        for status in self.fresh_peer_statuses():
            participant = (status.get("participant") or "").strip()
            if not participant or participant == self.pod_name:
                continue
            if status.get("role") != "control-plane":
                continue
            if not relay_status_ready(status):
                continue
            if not self.peer_frontdoor_eligible(participant):
                continue
            peers.append({
                "participant": participant,
                "host": self.auth_barrier_peer_host(participant),
            })
        peers.sort(key=lambda peer: peer["participant"])
        return peers

    def proxy_request_headers(self, barrier_name, request_headers, client_address, peer_subject):
        headers = {}
        for header_name, header_value in request_headers.items():
            header_lower = header_name.lower()
            if header_lower in HOP_BY_HOP_HEADERS or header_lower == "content-length":
                continue
            headers[header_name] = header_value

        forwarded_for = headers.get("X-Forwarded-For", "").strip()
        if client_address:
            headers["X-Forwarded-For"] = (
                f"{forwarded_for}, {client_address}" if forwarded_for else client_address
            )
        if barrier_name == "api":
            headers.setdefault("X-Forwarded-Proto", "https")
            if peer_subject:
                headers.setdefault("X-SSL-Subject", peer_subject)
                headers.setdefault("X-Client-DN", peer_subject)
                headers.setdefault("X-Client-Verify", "SUCCESS")
            else:
                headers.setdefault("X-Client-Verify", "NONE")
        return headers

    @staticmethod
    def proxy_upstream_request(scheme, host, port, method, path, headers, body, timeout, context=None):
        connection_class = (
            http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        )
        kwargs = {"timeout": timeout}
        if scheme == "https":
            kwargs["context"] = context
        connection = connection_class(host, port, **kwargs)
        try:
            connection.request(method, path, body=body if body else None, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            return response.status, response.reason, response.getheaders(), response_body
        finally:
            connection.close()

    def validate_session_cookie_on_peer(self, peer_host, cookie_header):
        status_code, _reason, _headers, body = self.proxy_upstream_request(
            "https",
            peer_host,
            443,
            "GET",
            "/",
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Cookie": cookie_header,
                "Host": peer_host,
            },
            body=b"",
            timeout=self.auth_barrier_peer_request_timeout_seconds,
            context=self.build_local_service_context(),
        )
        if status_code != 200:
            return False
        return response_is_console_page(body)

    def normalize_loginsession_id(self, session_id):
        value = (session_id or "").strip()
        if not value:
            raise RelayLocalCommandError(400, "loginsession id is required")
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise RelayLocalCommandError(400, f"invalid loginsession id: {value}") from error

    def serialize_loginsession_row(self, row):
        if not row:
            return None
        return {
            "id": self.normalize_loginsession_id(row["id"]),
            "creationDate": datetime_to_text(row["creationDate"]),
            "expirationDate": datetime_to_text(row["expirationDate"]),
        }

    def auth_barrier_loginsession_uses_cassandra(self):
        return self.auth_barrier_enabled

    def ensure_loginsession_cassandra_session(self):
        if not self.auth_barrier_enabled:
            raise RuntimeError("auth barrier is not enabled")
        if not self.auth_barrier_loginsession_cassandra_contact_points:
            raise RuntimeError("no Cassandra contact points configured for loginsession backend")
        if Cluster is None or ConsistencyLevel is None or SimpleStatement is None:
            raise RuntimeError("cassandra-driver is not available in the Conductor image")

        with self.lock:
            if self.auth_barrier_loginsession_cassandra_session is not None:
                return self.auth_barrier_loginsession_cassandra_session

            cluster = Cluster(
                contact_points=self.auth_barrier_loginsession_cassandra_contact_points,
                port=self.auth_barrier_loginsession_cassandra_port,
            )
            session = cluster.connect()
            keyspace = self.auth_barrier_loginsession_cassandra_keyspace
            table = self.auth_barrier_loginsession_cassandra_table
            replication_factor = max(
                1,
                self.auth_barrier_loginsession_cassandra_replication_factor,
            )
            session.execute(
                f"""
                create keyspace if not exists {keyspace}
                with replication = {{
                    'class': 'SimpleStrategy',
                    'replication_factor': {replication_factor}
                }}
                """
            )
            session.set_keyspace(keyspace)
            session.execute(
                f"""
                create table if not exists {table} (
                    id uuid primary key,
                    creation_date timestamp,
                    expiration_date timestamp
                )
                """
            )
            self.auth_barrier_loginsession_cassandra_cluster = cluster
            self.auth_barrier_loginsession_cassandra_session = session
            return session

    def read_shared_loginsession(self, session_id):
        session_id = self.normalize_loginsession_id(session_id)
        session = self.ensure_loginsession_cassandra_session()
        statement = SimpleStatement(
            (
                f"select id, creation_date, expiration_date from "
                f"{self.auth_barrier_loginsession_cassandra_table} where id = %s"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        row = session.execute(statement, (uuid.UUID(session_id),)).one()
        if row is None:
            return None
        return self.serialize_loginsession_row(
            {
                "id": str(row.id),
                "creationDate": row.creation_date,
                "expirationDate": row.expiration_date,
            }
        )

    def read_local_loginsession(self, session_id):
        session_id = self.normalize_loginsession_id(session_id)
        connection = self.rbac_db_connection()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    select id::text, creation_date, expiration_date
                    from loginsession
                    where id = %s::uuid
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        finally:
            connection.close()
        if row is None:
            return None
        return self.serialize_loginsession_row(
            {
                "id": row[0],
                "creationDate": row[1],
                "expirationDate": row[2],
            }
        )

    def wait_for_local_loginsession(self, session_id):
        deadline = time.time() + max(self.auth_barrier_wait_timeout_seconds, 1)
        while time.time() < deadline:
            payload = self.read_local_loginsession(session_id)
            if payload is not None:
                return payload
            time.sleep(self.auth_barrier_poll_interval_seconds)
        raise RuntimeError(f"timed out waiting for local loginsession {session_id}")

    def upsert_local_loginsession(self, payload, expected_session_id=""):
        session_id = self.normalize_loginsession_id(
            (payload or {}).get("id") or expected_session_id
        )
        if expected_session_id and session_id != self.normalize_loginsession_id(expected_session_id):
            raise RelayLocalCommandError(400, "loginsession id does not match request path")

        try:
            creation_date = parse_datetime_text((payload or {}).get("creationDate"))
            expiration_date = parse_datetime_text((payload or {}).get("expirationDate"))
        except ValueError as error:
            raise RelayLocalCommandError(400, f"invalid loginsession timestamp: {error}") from error

        if creation_date is None or expiration_date is None:
            raise RelayLocalCommandError(
                400,
                "loginsession creationDate and expirationDate are required",
            )

        connection = self.rbac_db_connection()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    insert into loginsession (id, creation_date, expiration_date)
                    values (%s::uuid, %s, %s)
                    on conflict (id) do update
                    set
                        creation_date = excluded.creation_date,
                        expiration_date = excluded.expiration_date
                    """,
                    (session_id, creation_date, expiration_date),
                )
            finally:
                cursor.close()
            connection.commit()
        finally:
            connection.close()
        return {
            "id": session_id,
            "creationDate": datetime_to_text(creation_date),
            "expirationDate": datetime_to_text(expiration_date),
        }

    def upsert_shared_loginsession(self, payload, expected_session_id=""):
        session_id = self.normalize_loginsession_id(
            (payload or {}).get("id") or expected_session_id
        )
        if expected_session_id and session_id != self.normalize_loginsession_id(expected_session_id):
            raise RelayLocalCommandError(400, "loginsession id does not match request path")

        try:
            creation_date = parse_datetime_text((payload or {}).get("creationDate"))
            expiration_date = parse_datetime_text((payload or {}).get("expirationDate"))
        except ValueError as error:
            raise RelayLocalCommandError(400, f"invalid loginsession timestamp: {error}") from error

        if creation_date is None or expiration_date is None:
            raise RelayLocalCommandError(
                400,
                "loginsession creationDate and expirationDate are required",
            )

        ttl_seconds = max(
            1,
            int((expiration_date - datetime.now(timezone.utc)).total_seconds()),
        )
        session = self.ensure_loginsession_cassandra_session()
        statement = SimpleStatement(
            (
                f"insert into {self.auth_barrier_loginsession_cassandra_table} "
                "(id, creation_date, expiration_date) values (%s, %s, %s) using ttl %s"
            ),
            consistency_level=ConsistencyLevel.QUORUM,
        )
        session.execute(
            statement,
            (
                uuid.UUID(session_id),
                creation_date,
                expiration_date,
                ttl_seconds,
            ),
        )
        return {
            "id": session_id,
            "creationDate": datetime_to_text(creation_date),
            "expirationDate": datetime_to_text(expiration_date),
        }

    def ensure_local_loginsession_from_shared_state(self, session_id):
        session_id = self.normalize_loginsession_id(session_id)
        if self.read_local_loginsession(session_id) is not None:
            return True
        payload = self.read_shared_loginsession(session_id)
        if payload is None:
            return False
        self.upsert_local_loginsession(payload, expected_session_id=session_id)
        return True

    def synchronize_loginsession(self, session_id):
        session_payload = self.wait_for_local_loginsession(session_id)
        self.upsert_shared_loginsession(session_payload, expected_session_id=session_id)

    def validate_bearer_token_on_peer(self, peer_host, token):
        status_code, _reason, _headers, _body = self.proxy_upstream_request(
            "https",
            peer_host,
            self.auth_barrier_api_port,
            "GET",
            "/rbac-api/v1/users/current",
            headers={
                "Accept": "application/json",
                "Host": peer_host,
                "X-Authentication": token,
            },
            body=b"",
            timeout=self.auth_barrier_peer_request_timeout_seconds,
            context=self.build_local_service_context(),
        )
        return status_code == 200

    def wait_for_auth_replication(self, auth_kind, credential):
        peers = self.auth_barrier_target_peers()
        if not peers:
            return

        validator = (
            self.validate_session_cookie_on_peer
            if auth_kind == "session"
            else self.validate_bearer_token_on_peer
        )
        pending = {peer["participant"]: peer["host"] for peer in peers}
        last_errors = {}
        deadline = time.time() + max(self.auth_barrier_wait_timeout_seconds, 1)
        while pending and time.time() < deadline:
            for participant, host in list(pending.items()):
                try:
                    if validator(host, credential):
                        pending.pop(participant, None)
                        last_errors.pop(participant, None)
                        continue
                except Exception as error:  # pragma: no cover - transient network failures
                    last_errors[participant] = str(error)
            if pending:
                time.sleep(self.auth_barrier_poll_interval_seconds)

        if pending:
            details = ", ".join(
                f"{participant} ({last_errors.get(participant, 'not yet converged')})"
                for participant in sorted(pending)
            )
            raise RuntimeError(f"timed out waiting for auth convergence on {details}")

    def auth_barrier_proxy_target(self, barrier_name):
        if barrier_name == "console":
            return (
                "http",
                self.auth_barrier_http_target_host,
                self.auth_barrier_http_target_port,
            )
        if barrier_name == "api":
            return (
                "http",
                self.auth_barrier_api_target_host,
                self.auth_barrier_api_target_port,
            )
        raise RelayLocalCommandError(404, f"unknown auth barrier target: {barrier_name}")

    def parse_auth_barrier_token(self, body):
        if not body:
            return ""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, dict):
            return (payload.get("token") or "").strip()
        return ""

    def handle_local_auth_barrier_request(
        self,
        barrier_name,
        method,
        parsed,
        request_headers,
        body,
        *,
        peer_subject="",
        client_address="",
    ):
        scheme, host, port = self.auth_barrier_proxy_target(barrier_name)
        path = request_path_with_query(parsed)
        proxy_headers = self.proxy_request_headers(
            barrier_name,
            request_headers,
            client_address,
            peer_subject,
        )

        if barrier_name == "console":
            session_id = extract_request_cookie(
                request_headers,
                DEFAULT_AUTH_BARRIER_SESSION_COOKIE_NAME,
            )
            if session_id:
                try:
                    self.ensure_local_loginsession_from_shared_state(session_id)
                except RelayLocalCommandError:
                    raise
                except Exception as error:
                    self.auth_barrier_last_error = str(error)
                    self.set_status(authBarrierLastError=str(error))
                    raise RelayLocalCommandError(
                        503,
                        f"auth loginsession lookup failed: {error}",
                    ) from error
        if barrier_name == "api":
            auth_token = extract_request_auth_token(request_headers)
            if auth_token:
                try:
                    self.ensure_local_rbac_token_from_shared_state(auth_token)
                except RelayLocalCommandError:
                    raise
                except Exception as error:
                    self.auth_barrier_last_error = str(error)
                    self.set_status(authBarrierLastError=str(error))
                    raise RelayLocalCommandError(
                        503,
                        f"auth token lookup failed: {error}",
                    ) from error

        try:
            status_code, _reason, response_headers, response_body = self.proxy_upstream_request(
                scheme,
                host,
                port,
                method,
                path,
                headers=proxy_headers,
                body=body,
                timeout=self.auth_barrier_request_timeout_seconds,
            )
        except RelayLocalCommandError:
            raise
        except Exception as error:
            self.auth_barrier_last_error = str(error)
            self.set_status(authBarrierLastError=str(error))
            raise RelayLocalCommandError(502, f"auth barrier upstream request failed: {error}") from error

        if not self.auth_barrier_enabled:
            return status_code, response_headers, response_body

        if barrier_name == "console" and 200 <= status_code < 400:
            session_cookie = extract_response_cookie(
                response_headers,
                DEFAULT_AUTH_BARRIER_SESSION_COOKIE_NAME,
            )
            if session_cookie:
                session_parts = session_cookie.split("=", 1)
                session_id = session_parts[1].strip() if len(session_parts) == 2 else ""
                if session_id:
                    try:
                        self.synchronize_loginsession(session_id)
                        self.auth_barrier_last_error = ""
                        self.set_status(authBarrierLastError="")
                    except Exception as error:
                        self.auth_barrier_last_error = str(error)
                        self.set_status(authBarrierLastError=str(error))
                        raise RelayLocalCommandError(
                            503,
                            f"auth issuance did not converge: {error}",
                        ) from error

            auth_cookie = extract_response_cookie(
                response_headers,
                DEFAULT_AUTH_BARRIER_AUTH_COOKIE_NAME,
            )
            if auth_cookie:
                try:
                    if self.rbac_token_sync_enabled:
                        self.publish_rbac_token_state_now()
                    else:
                        self.publish_rbac_state_now()
                    self.wait_for_auth_replication("session", auth_cookie)
                    self.auth_barrier_last_error = ""
                    self.set_status(authBarrierLastError="")
                except Exception as error:
                    self.auth_barrier_last_error = str(error)
                    self.set_status(authBarrierLastError=str(error))
                    raise RelayLocalCommandError(
                        503,
                        f"auth issuance did not converge: {error}",
                    ) from error

        if not (barrier_name == "api" and method == "POST"):
            return status_code, response_headers, response_body

        auth_kind = ""
        credential = ""
        if parsed.path == DEFAULT_AUTH_BARRIER_TOKEN_PATH and status_code == 200:
            credential = self.parse_auth_barrier_token(response_body)
            if credential:
                auth_kind = "token"

        if not auth_kind or not credential:
            return status_code, response_headers, response_body

        try:
            if self.rbac_token_sync_enabled and auth_kind == "token":
                self.publish_rbac_token_state_now()
            else:
                self.publish_rbac_state_now()
            self.wait_for_auth_replication(auth_kind, credential)
            self.auth_barrier_last_error = ""
            self.set_status(authBarrierLastError="")
        except Exception as error:
            self.auth_barrier_last_error = str(error)
            self.set_status(authBarrierLastError=str(error))
            raise RelayLocalCommandError(503, f"auth issuance did not converge: {error}") from error

        return status_code, response_headers, response_body

    def ensure_auth_barrier_running(self):
        if not self.auth_barrier_enabled:
            return

        http_ready = (
            self.auth_barrier_http_server is not None
            and self.auth_barrier_http_thread is not None
            and self.auth_barrier_http_thread.is_alive()
        )
        api_ready = (
            self.auth_barrier_api_server is not None
            and self.auth_barrier_api_thread is not None
            and self.auth_barrier_api_thread.is_alive()
        )
        if http_ready and api_ready:
            self.auth_barrier_start_error = ""
            self.set_status(
                authBarrierReady=True,
                authBarrierLastError=self.auth_barrier_last_error,
            )
            return

        try:
            if not http_ready:
                server = ThreadingHTTPServer(
                    (self.auth_barrier_http_listen_host, self.auth_barrier_http_port),
                    LocalAuthBarrierHandler,
                )
                server.runtime = self
                server.barrier_name = "console"
                server.daemon_threads = True
                thread = threading.Thread(
                    target=server.serve_forever,
                    name="conductor-relay-auth-http",
                    daemon=True,
                )
                thread.start()
                self.auth_barrier_http_server = server
                self.auth_barrier_http_thread = thread
                log(
                    "Started auth barrier HTTP proxy on "
                    f"{self.auth_barrier_http_listen_host}:{self.auth_barrier_http_port}"
                )

            if not api_ready:
                server = ThreadingHTTPServer(
                    (self.auth_barrier_api_listen_host, self.auth_barrier_api_port),
                    LocalAuthBarrierHandler,
                )
                server.runtime = self
                server.barrier_name = "api"
                server.daemon_threads = True
                context = self.build_auth_barrier_server_context()
                server.socket = context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(
                    target=server.serve_forever,
                    name="conductor-relay-auth-api",
                    daemon=True,
                )
                thread.start()
                self.auth_barrier_api_server = server
                self.auth_barrier_api_thread = thread
                log(
                    "Started auth barrier API proxy on "
                    f"{self.auth_barrier_api_listen_host}:{self.auth_barrier_api_port}"
                )

            self.auth_barrier_start_error = ""
            self.set_status(
                authBarrierReady=True,
                authBarrierLastError=self.auth_barrier_last_error,
            )
        except Exception as error:
            if str(error) != self.auth_barrier_start_error:
                log(f"Auth barrier startup failed: {error}")
                self.auth_barrier_start_error = str(error)
            self.set_status(
                authBarrierReady=False,
                authBarrierLastError=str(error),
            )

    def ensure_code_deploy_hook_running(self):
        if not self.code_deploy_enabled:
            return

        if (
            self.code_deploy_hook_server is not None
            and self.code_deploy_hook_thread is not None
            and self.code_deploy_hook_thread.is_alive()
        ):
            self.code_deploy_hook_start_error = ""
            self.code_deploy_runtime_error = ""
            self.refresh_code_deploy_summary()
            return

        try:
            _, cert_path, key_path, _ = self.puppet_ssl_paths()
            server = ThreadingHTTPServer(
                (self.code_deploy_hook_listen_host, self.code_deploy_hook_listen_port),
                LocalCodeDeployHookHandler,
            )
            server.runtime = self
            server.daemon_threads = True
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=cert_path, keyfile=key_path)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(
                target=server.serve_forever,
                name="conductor-relay-code-deploy-hook",
                daemon=True,
            )
            thread.start()
            self.code_deploy_hook_server = server
            self.code_deploy_hook_thread = thread
            self.code_deploy_hook_start_error = ""
            self.code_deploy_runtime_error = ""
            self.refresh_code_deploy_summary()
            log(
                "Started code deploy hook listener on "
                f"{self.code_deploy_hook_listen_host}:{self.code_deploy_hook_listen_port}"
            )
        except Exception as error:
            if str(error) != self.code_deploy_hook_start_error:
                log(f"Code deploy hook startup failed: {error}")
                self.code_deploy_hook_start_error = str(error)
            self.code_deploy_runtime_error = str(error)
            self.refresh_code_deploy_summary()

    def issue_code_deploy_token(self):
        return self.issue_service_token(
            self.code_deploy_service_host,
            self.code_deploy_username,
            self.code_deploy_password,
            self.code_deploy_token_lifetime,
            self.code_deploy_token_label,
        )

    def code_manager_request(self, method, path, payload=None):
        token = self.issue_code_deploy_token()
        context = self.build_local_service_context()
        status_code, body = http_request_json(
            method,
            f"https://{self.code_deploy_service_host}:8170/code-manager/v1{path}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Authentication": token,
            },
            payload=payload,
            context=context,
            timeout=self.code_deploy_request_timeout_seconds,
        )
        if not body:
            return status_code, None
        try:
            return status_code, json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Code Manager {method} {path} returned non-JSON response: {body}"
            ) from error

    def refresh_local_code_deploy_status(self, force=False):
        if not self.code_deploy_enabled:
            return

        now = int(time.time())
        if not force and now - self.last_code_deploy_status_poll < self.code_deploy_status_poll_interval:
            return

        status_code, payload = self.code_manager_request("GET", "/deploys/status")
        if status_code != 200:
            raise RuntimeError(f"Code Manager status returned {status_code}: {payload}")

        for environment, result in self.extract_code_manager_status_entries(payload).items():
            desired_signature = (
                self.code_deploy_state_for(environment).get("desiredSignature") or ""
            ).strip()
            actual_signature = (result.get("deploySignature") or "").strip()
            updates = {
                "actualSignature": actual_signature,
                "actualDeployId": int(result.get("deployId") or 0),
                "actualCodeCommit": (result.get("codeCommit") or "").strip(),
                "actualEnvironmentCommit": (result.get("environmentCommit") or "").strip(),
                "lastStatusPollAt": now,
            }
            if desired_signature and actual_signature == desired_signature:
                updates.update(
                    {
                        "state": "converged",
                        "lastConvergedAt": now,
                        "lastError": "",
                    }
                )
            elif desired_signature and actual_signature != desired_signature:
                current_state = (self.code_deploy_state_for(environment).get("state") or "").strip()
                updates["state"] = current_state if current_state in {"pending", "in-progress", "failed"} else "stale"
            elif actual_signature:
                updates.update(
                    {
                        "state": "observed",
                        "lastError": "",
                    }
                )
            self.merge_code_deploy_state(environment, **updates)

        self.last_code_deploy_status_poll = now
        self.code_deploy_runtime_error = ""
        self.refresh_code_deploy_summary()

    def publish_code_deploy_intent(self, environment, hook_payload, published_at):
        environment = (environment or "").strip()
        result = self.extract_code_deploy_result(hook_payload, environment=environment)
        deploy_signature = (result.get("deploySignature") or "").strip()
        if not deploy_signature:
            raise RuntimeError(f"code deploy hook for {environment} did not include a deploy signature")

        payload = {
            "apiVersion": "pe-k8s.puppet.com/v1alpha1",
            "kind": "ConductorRelayCodeDeployIntent",
            "publishedAt": int(published_at),
            "origin": {
                "participant": self.pod_name,
                "namespace": self.pod_namespace,
                "role": self.relay_role,
                "segment": self.segment_name,
            },
            "targetRoles": list(self.code_deploy_target_roles),
            "environment": environment,
            "deploy": {
                "environment": environment,
                "id": int(result.get("deployId") or 0),
                "status": (result.get("status") or "complete").strip() or "complete",
                "deploySignature": deploy_signature,
                "fileSync": {
                    "environmentCommit": (result.get("environmentCommit") or "").strip(),
                    "codeCommit": (result.get("codeCommit") or "").strip(),
                },
            },
        }
        self.publish_envelope(
            f"relay.code-deploy.{sanitize_fragment(environment)}",
            payload,
        )
        return result

    def handle_local_code_deploy_hook(self, payload):
        if not self.code_deploy_enabled:
            raise RelayLocalCommandError(404, "code deploy hooks are disabled")

        environment = (payload.get("environment") or "").strip()
        if not environment:
            raise RelayLocalCommandError(400, "code deploy hook is missing environment")

        result = self.extract_code_deploy_result(payload, environment=environment)
        status_value = (result.get("status") or "").strip()
        if status_value and status_value != "complete":
            return {
                "environment": environment,
                "published": False,
                "suppressed": False,
                "ignored": True,
                "status": status_value,
            }

        deploy_signature = (result.get("deploySignature") or "").strip()
        if not deploy_signature:
            raise RelayLocalCommandError(400, "code deploy hook is missing deploy signature")

        published_at = int(time.time())
        if self.hook_is_suppressed(environment, deploy_signature):
            self.clear_code_deploy_suppression(environment)
            desired_signature = (
                self.code_deploy_state_for(environment).get("desiredSignature") or ""
            ).strip()
            updates = {
                "actualSignature": deploy_signature,
                "actualDeployId": int(result.get("deployId") or 0),
                "actualCodeCommit": (result.get("codeCommit") or "").strip(),
                "actualEnvironmentCommit": (result.get("environmentCommit") or "").strip(),
            }
            if desired_signature and deploy_signature == desired_signature:
                updates.update(
                    {
                        "state": "converged",
                        "lastConvergedAt": published_at,
                        "lastError": "",
                    }
                )
            elif desired_signature and deploy_signature != desired_signature:
                updates.update(
                    {
                        "state": "failed",
                        "lastError": (
                            "local replay converged to the wrong deploy signature: "
                            f"expected {desired_signature}, got {deploy_signature}"
                        ),
                    }
                )
            else:
                updates.update(
                    {
                        "state": "observed",
                        "lastError": "",
                    }
                )
            self.merge_code_deploy_state(environment, **updates)
            log(
                f"Suppressed replay hook for {environment} at {deploy_signature[:12]}"
            )
            return {
                "environment": environment,
                "deploySignature": deploy_signature,
                "published": False,
                "suppressed": True,
            }

        try:
            self.publish_code_deploy_intent(environment, payload, published_at)
            self.code_deploy_runtime_error = ""
        except Exception as error:
            self.code_deploy_runtime_error = str(error)
            self.merge_code_deploy_state(
                environment,
                state="failed",
                desiredSignature=deploy_signature,
                actualSignature=deploy_signature,
                desiredDeployId=int(result.get("deployId") or 0),
                actualDeployId=int(result.get("deployId") or 0),
                desiredCodeCommit=(result.get("codeCommit") or "").strip(),
                actualCodeCommit=(result.get("codeCommit") or "").strip(),
                desiredEnvironmentCommit=(result.get("environmentCommit") or "").strip(),
                actualEnvironmentCommit=(result.get("environmentCommit") or "").strip(),
                originParticipant=self.pod_name,
                desiredPublishedAt=published_at,
                lastPublishedAt=published_at,
                lastError=f"failed to publish code deploy intent: {error}",
            )
            raise RelayLocalCommandError(503, f"failed to publish code deploy intent: {error}") from error

        self.merge_code_deploy_state(
            environment,
            state="converged",
            desiredSignature=deploy_signature,
            actualSignature=deploy_signature,
            desiredDeployId=int(result.get("deployId") or 0),
            actualDeployId=int(result.get("deployId") or 0),
            desiredCodeCommit=(result.get("codeCommit") or "").strip(),
            actualCodeCommit=(result.get("codeCommit") or "").strip(),
            desiredEnvironmentCommit=(result.get("environmentCommit") or "").strip(),
            actualEnvironmentCommit=(result.get("environmentCommit") or "").strip(),
            originParticipant=self.pod_name,
            desiredPublishedAt=published_at,
            lastPublishedAt=published_at,
            lastConvergedAt=published_at,
            lastError="",
        )
        log(f"Published code deploy intent for {environment} at {deploy_signature[:12]}")
        return {
            "environment": environment,
            "deploySignature": deploy_signature,
            "published": True,
            "suppressed": False,
        }

    def handle_remote_code_deploy_intent(self, payload):
        if not self.code_deploy_enabled:
            return

        origin = payload.get("origin") or {}
        if (origin.get("participant") or "").strip() == self.pod_name:
            return

        target_roles = payload.get("targetRoles") or []
        if target_roles and self.relay_role not in target_roles:
            return

        deploy = payload.get("deploy") or {}
        environment = (payload.get("environment") or deploy.get("environment") or "").strip()
        if not environment:
            raise RuntimeError("code deploy intent is missing environment")

        result = self.extract_code_deploy_result(
            {
                "environment": environment,
                "id": deploy.get("id"),
                "status": deploy.get("status"),
                "deploySignature": deploy.get("deploySignature"),
                "fileSync": deploy.get("fileSync"),
            },
            environment=environment,
        )
        desired_signature = (result.get("deploySignature") or "").strip()
        if not desired_signature:
            raise RuntimeError(f"code deploy intent for {environment} is missing deploy signature")

        published_at = int(payload.get("publishedAt") or 0)
        current_state = self.code_deploy_state_for(environment)
        current_published_at = int(current_state.get("desiredPublishedAt") or 0)
        if current_published_at and published_at and published_at < current_published_at:
            return
        if (
            current_published_at
            and published_at
            and published_at == current_published_at
            and desired_signature == (current_state.get("desiredSignature") or "").strip()
        ):
            return

        actual_signature = (current_state.get("actualSignature") or "").strip()
        updates = {
            "desiredSignature": desired_signature,
            "desiredDeployId": int(result.get("deployId") or 0),
            "desiredCodeCommit": (result.get("codeCommit") or "").strip(),
            "desiredEnvironmentCommit": (result.get("environmentCommit") or "").strip(),
            "originParticipant": (origin.get("participant") or "").strip(),
            "desiredPublishedAt": published_at or int(time.time()),
            "lastReceivedAt": int(time.time()),
            "lastError": "",
        }
        if actual_signature and actual_signature == desired_signature:
            updates.update(
                {
                    "state": "converged",
                    "lastConvergedAt": int(time.time()),
                }
            )
        else:
            updates["state"] = "pending"

        self.merge_code_deploy_state(environment, **updates)
        log(
            "Received code deploy intent for "
            f"{environment} at {desired_signature[:12]} from {origin.get('participant') or 'unknown'}"
        )
        if actual_signature != desired_signature:
            self.schedule_code_deploy(environment)

    def request_local_code_deploy(self, environment):
        status_code, payload = self.code_manager_request(
            "POST",
            "/deploys",
            payload={
                "environments": [environment],
                "wait": True,
            },
        )
        if status_code not in {200, 202}:
            raise RuntimeError(f"Code Manager deploy returned {status_code}: {payload}")

        if isinstance(payload, list):
            for item in payload:
                result = self.extract_code_deploy_result(item, environment=environment)
                if (result.get("environment") or "").strip() == environment:
                    break
            else:
                result = self.extract_code_deploy_result(payload[0] if payload else {}, environment=environment)
        else:
            result = self.extract_code_deploy_result(payload, environment=environment)

        if (result.get("status") or "").strip() != "complete":
            raise RuntimeError(
                f"Code Manager deploy for {environment} did not complete successfully: {result}"
            )
        if not (result.get("deploySignature") or "").strip():
            raise RuntimeError(f"Code Manager deploy for {environment} did not return a deploy signature")
        return result

    def reconcile_code_deploy_environment(self, environment):
        environment = (environment or "").strip()
        if not environment:
            return

        state = self.code_deploy_state_for(environment)
        desired_signature = (state.get("desiredSignature") or "").strip()
        actual_signature = (state.get("actualSignature") or "").strip()
        if not desired_signature:
            return
        if actual_signature and actual_signature == desired_signature:
            self.merge_code_deploy_state(
                environment,
                state="converged",
                lastConvergedAt=int(time.time()),
                lastError="",
            )
            return

        attempt_at = int(time.time())
        self.merge_code_deploy_state(
            environment,
            state="in-progress",
            lastRequestedAt=attempt_at,
            lastAttemptAt=attempt_at,
            lastError="",
        )
        self.suppress_code_deploy_hook(environment, desired_signature)

        try:
            result = self.request_local_code_deploy(environment)
            actual_signature = (result.get("deploySignature") or "").strip()
            self.narrow_code_deploy_suppression(environment, actual_signature or desired_signature)

            updates = {
                "actualSignature": actual_signature,
                "actualDeployId": int(result.get("deployId") or 0),
                "actualCodeCommit": (result.get("codeCommit") or "").strip(),
                "actualEnvironmentCommit": (result.get("environmentCommit") or "").strip(),
                "lastAttemptAt": int(time.time()),
            }
            if actual_signature != desired_signature:
                updates.update(
                    {
                        "state": "failed",
                        "lastError": (
                            "local replay converged to the wrong deploy signature: "
                            f"expected {desired_signature}, got {actual_signature or 'none'}"
                        ),
                    }
                )
            else:
                updates.update(
                    {
                        "state": "converged",
                        "lastConvergedAt": int(time.time()),
                        "lastError": "",
                    }
                )
            self.merge_code_deploy_state(environment, **updates)
            self.code_deploy_runtime_error = ""
            log(
                f"Replayed code deploy for {environment} at {actual_signature[:12]}"
            )
        except Exception as error:
            self.code_deploy_runtime_error = str(error)
            self.merge_code_deploy_state(
                environment,
                state="failed",
                lastAttemptAt=int(time.time()),
                lastError=str(error),
            )
            log(f"Code deploy replay failed for {environment}: {error}")

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

        if kind == "ConductorRelayCodeDeployIntent":
            try:
                self.handle_remote_code_deploy_intent(payload)
                self.set_status(lastReceivedAt=int(time.time()))
                channel.basic_ack(method.delivery_tag)
            except Exception as error:
                self.code_deploy_runtime_error = str(error)
                self.refresh_code_deploy_summary()
                log(f"Remote code deploy intent handling failed: {error}")
                time.sleep(2)
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

        if kind == "ConductorRelayClassifierState":
            try:
                self.handle_remote_classifier_state(payload)
                self.set_status(lastReceivedAt=int(time.time()))
                channel.basic_ack(method.delivery_tag)
            except Exception as error:
                self.classifier_sync_runtime_error = str(error)
                self.refresh_classifier_summary()
                log(f"Remote classifier sync handling failed: {error}")
                time.sleep(2)
                channel.basic_nack(method.delivery_tag, requeue=True)
            return

        if kind == "ConductorRelayRbacState":
            try:
                self.handle_remote_rbac_state(payload)
                self.set_status(lastReceivedAt=int(time.time()))
                channel.basic_ack(method.delivery_tag)
            except Exception as error:
                self.rbac_sync_runtime_error = str(error)
                self.refresh_rbac_summary()
                log(f"Remote RBAC sync handling failed: {error}")
                time.sleep(2)
                channel.basic_nack(method.delivery_tag, requeue=True)
            return

        if kind == "ConductorRelayRbacTokenState":
            try:
                self.handle_remote_rbac_token_state(payload)
                self.set_status(lastReceivedAt=int(time.time()))
                channel.basic_ack(method.delivery_tag)
            except Exception as error:
                self.rbac_token_sync_runtime_error = str(error)
                self.refresh_rbac_token_summary()
                log(f"Remote RBAC token sync handling failed: {error}")
                time.sleep(2)
                channel.basic_nack(method.delivery_tag, requeue=True)
            return

        if kind == "ConductorRelayOrchestrationState":
            try:
                self.handle_remote_orchestration_state(payload)
                self.set_status(lastReceivedAt=int(time.time()))
                channel.basic_ack(method.delivery_tag)
            except Exception as error:
                self.orchestration_sync_runtime_error = str(error)
                self.refresh_orchestration_summary()
                log(f"Remote orchestration sync handling failed: {error}")
                time.sleep(2)
                channel.basic_nack(method.delivery_tag, requeue=True)
            return

        if kind == "ConductorRelayInventoryState":
            try:
                self.handle_remote_inventory_state(payload)
                self.set_status(lastReceivedAt=int(time.time()))
                channel.basic_ack(method.delivery_tag)
            except Exception as error:
                self.inventory_sync_runtime_error = str(error)
                self.refresh_inventory_summary()
                log(f"Remote inventory sync handling failed: {error}")
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
        self.next_classifier_publish = 0
        self.next_rbac_publish = 0
        self.next_orchestration_publish = 0
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
        self.refresh_classifier_summary()
        self.refresh_rbac_summary()
        self.refresh_orchestration_summary()
        self.refresh_code_deploy_summary()
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
                self.ensure_code_deploy_hook_running()
                self.ensure_auth_barrier_running()
                self.start_code_deploy_worker()
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
                self.refresh_local_classifier_sync_state()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                self.classifier_sync_runtime_error = str(error)
                self.refresh_classifier_summary()
                log(f"Classifier sync refresh failed: {error}")

            try:
                self.refresh_local_rbac_sync_state()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                self.rbac_sync_runtime_error = str(error)
                self.refresh_rbac_summary()
                log(f"RBAC sync refresh failed: {error}")

            try:
                self.refresh_local_rbac_token_sync_state()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                self.rbac_token_sync_runtime_error = str(error)
                self.refresh_rbac_token_summary()
                log(f"RBAC token sync refresh failed: {error}")

            try:
                self.refresh_local_orchestration_sync_state()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                self.orchestration_sync_runtime_error = str(error)
                self.refresh_orchestration_summary()
                log(f"Orchestration sync refresh failed: {error}")

            try:
                self.refresh_local_inventory_sync_state()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                self.inventory_sync_runtime_error = str(error)
                self.refresh_inventory_summary()
                log(f"Inventory sync refresh failed: {error}")

            try:
                self.refresh_local_code_deploy_status()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                last_error = str(error)
                self.code_deploy_runtime_error = str(error)
                self.refresh_code_deploy_summary()
                log(f"Code deploy status refresh failed: {error}")

            self.refresh_frontdoor_summary()

            try:
                self.refresh_onboarding(force=self.connection is None or not self.connection.is_open)
                if self.connection is not None and self.connection.is_open:
                    self.publish_status()
                    self.publish_ca_state()
                    self.publish_classifier_state()
                    self.publish_rbac_state()
                    self.publish_rbac_token_state()
                    self.publish_orchestration_state()
                    self.publish_inventory_state()
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
            try:
                self.publish_frontdoor_status()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                log(f"Front door status publish loop failed: {error}")


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
