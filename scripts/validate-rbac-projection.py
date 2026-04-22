#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import time
import uuid


def run(args, *, input_text=None, timeout=90):
    completed = subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )
    return completed.stdout.strip()


def exec_sh(namespace, pod, container, script):
    return run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            pod,
            "-c",
            container,
            "--",
            "/bin/sh",
            "-lc",
            script,
        ],
    )


def issue_admin_token(namespace, pod):
    script = r"""
set -e
password=$(python3 - <<'INNERPY'
import re
for path in ['/var/lib/pe-k8s/install/pe.conf', '/etc/puppetlabs/enterprise.conf']:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            content = handle.read()
    except OSError:
        continue
    match = re.search(r'"console_admin_password"\s*=\s*"([^"]+)"', content)
    if match:
        print(match.group(1))
        raise SystemExit(0)
raise SystemExit(1)
INNERPY
)
cat > /tmp/pe-k8s-token.json <<EOF
{"login":"admin","password":"${password}","lifetime":"5m","label":"pe-k8s-conductor-rbac-proof-$(date +%s%N)"}
EOF
curl -sk --max-time 30 -H 'Content-Type: application/json' --request POST https://127.0.0.1:4433/rbac-api/v1/auth/token --data @/tmp/pe-k8s-token.json
"""
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            response = exec_sh(namespace, pod, "puppetserver", script)
            return json.loads(response)["token"]
        except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError):
            time.sleep(2)
    raise SystemExit(f"timed out issuing admin token on {pod}")


def api_json(namespace, pod, token, method, path, payload=None):
    script_lines = ["set -e"]
    if payload is not None:
        script_lines.append("cat > /tmp/pe-k8s-api.json <<'EOF'")
        script_lines.append(json.dumps(payload))
        script_lines.append("EOF")
        data_arg = " --data @/tmp/pe-k8s-api.json"
    else:
        data_arg = ""
    script_lines.append(
        "curl -sk --max-time 30 -H 'Accept: application/json' -H 'Content-Type: application/json' "
        f"-H 'X-Authentication: {token}' --request {method} https://127.0.0.1:4433{path}{data_arg}"
    )
    return json.loads(exec_sh(namespace, pod, "puppetserver", "\n".join(script_lines) + "\n"))


def api_status(namespace, pod, token, method, path, payload=None):
    script_lines = ["set -e"]
    if payload is not None:
        script_lines.append("cat > /tmp/pe-k8s-api.json <<'EOF'")
        script_lines.append(json.dumps(payload))
        script_lines.append("EOF")
        data_arg = " --data @/tmp/pe-k8s-api.json"
    else:
        data_arg = ""
    script_lines.append(
        "curl -sk --max-time 30 -o /dev/null -w '%{http_code}' "
        "-H 'Accept: application/json' -H 'Content-Type: application/json' "
        f"-H 'X-Authentication: {token}' --request {method} https://127.0.0.1:4433{path}{data_arg}"
    )
    return exec_sh(namespace, pod, "puppetserver", "\n".join(script_lines) + "\n")


def api_status_safe(namespace, pod, token, method, path, payload=None):
    try:
        return api_status(namespace, pod, token, method, path, payload)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "000"


def api_status_and_headers(namespace, pod, token, method, path, payload=None):
    script_lines = ["set -e"]
    if payload is not None:
        script_lines.append("cat > /tmp/pe-k8s-api.json <<'EOF'")
        script_lines.append(json.dumps(payload))
        script_lines.append("EOF")
        data_arg = " --data @/tmp/pe-k8s-api.json"
    else:
        data_arg = ""
    script_lines.append(
        "curl -sk --max-time 30 -D - -o /dev/null "
        "-H 'Accept: application/json' -H 'Content-Type: application/json' "
        f"-H 'X-Authentication: {token}' --request {method} "
        f"https://127.0.0.1:4433{path}{data_arg}"
    )
    output = exec_sh(namespace, pod, "puppetserver", "\n".join(script_lines) + "\n")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    status = ""
    headers = {}
    for line in lines:
        if line.startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2:
                status = parts[1]
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return status, headers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--writer-pod", required=True)
    parser.add_argument("--reader-pod", required=True)
    parser.add_argument("--wait-seconds", type=int, required=True)
    args = parser.parse_args()

    namespace = args.namespace
    writer_pod = args.writer_pod
    reader_pod = args.reader_pod
    wait_seconds = args.wait_seconds

    user_login = f"rbacproof{int(time.time())}"
    user_password = "TempProofPassw0rd!"
    user_display = "RBAC Proof"
    user_email = f"{user_login}@example.test"
    user_id = ""
    writer_admin = ""
    reader_admin = ""

    try:
        writer_admin = issue_admin_token(namespace, writer_pod)
        reader_admin = issue_admin_token(namespace, reader_pod)

        status, headers = api_status_and_headers(
            namespace,
            writer_pod,
            writer_admin,
            "POST",
            "/rbac-api/v1/users",
            {
                "login": user_login,
                "display_name": user_display,
                "email": user_email,
                "password": user_password,
                "role_ids": [1],
            },
        )
        if status not in {"200", "201", "202", "204", "303"}:
            raise SystemExit(f"unexpected RBAC create status on {writer_pod}: {status}")
        print(f"[info] created temp RBAC user {user_login} on {writer_pod} (status {status})", flush=True)

        location = headers.get("location", "").strip()
        user_id = location.rsplit("/", 1)[-1] if location else ""
        if not user_id:
            raise SystemExit(f"RBAC create response on {writer_pod} did not include a user id")

        deadline = time.time() + wait_seconds
        writer_ready = False
        reader_ready = False
        while time.time() < deadline:
            writer_status = api_status_safe(
                namespace,
                writer_pod,
                writer_admin,
                "GET",
                f"/rbac-api/v1/users/{user_id}",
            )
            reader_status = api_status_safe(
                namespace,
                reader_pod,
                reader_admin,
                "GET",
                f"/rbac-api/v1/users/{user_id}",
            )
            writer_ready = writer_status == "200"
            reader_ready = reader_status == "200"
            if writer_ready and reader_ready:
                break
            time.sleep(5)

        if not writer_ready:
            raise SystemExit(f"RBAC user {user_login} did not appear on {writer_pod}")
        if not reader_ready:
            raise SystemExit(f"RBAC user {user_login} did not appear on {reader_pod}")

        print(
            f"[info] RBAC user {user_login} is visible on {writer_pod} and {reader_pod}",
            flush=True,
        )

        reader_user_token = ""
        reader_identity = {}
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                reader_user_token = api_json(
                    namespace,
                    reader_pod,
                    reader_admin,
                    "POST",
                    "/rbac-api/v1/auth/token",
                    {
                        "login": user_login,
                        "password": user_password,
                        "lifetime": "5m",
                        "label": f"pe-k8s-rbac-proof-user-{uuid.uuid4()}",
                    },
                )["token"]
                reader_identity = api_json(
                    namespace,
                    reader_pod,
                    reader_user_token,
                    "GET",
                    "/rbac-api/v1/users/current",
                )
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, json.JSONDecodeError):
                time.sleep(5)
        if not reader_user_token:
            raise SystemExit(f"RBAC user {user_login} did not authenticate on {reader_pod}")
        if (reader_identity.get("login") or "").strip() != user_login:
            raise SystemExit(
                f"RBAC user token on {reader_pod} resolved to "
                f"{reader_identity.get('login')!r}, expected {user_login!r}"
            )

        print(
            f"[info] RBAC user {user_login} authenticated on {reader_pod}",
            flush=True,
        )

        print(
            f"[ok] RBAC user {user_login} is visible on {writer_pod} and {reader_pod}, "
            f"and authenticates on {reader_pod}",
            flush=True,
        )
    finally:
        if user_id:
            print(f"[info] deleting temp RBAC user {user_login}", flush=True)
            for pod, token in ((writer_pod, writer_admin), (reader_pod, reader_admin)):
                if not token:
                    continue
                try:
                    status = api_status(
                        namespace,
                        pod,
                        token,
                        "DELETE",
                        f"/rbac-api/v1/users/{user_id}",
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    continue
                if status in {"204", "404"}:
                    print(
                        f"[info] deleted temp RBAC user {user_login} via {pod} ({status})",
                        flush=True,
                    )
                    break


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired as error:
        raise SystemExit(f"timed out running kubectl exec: {error}") from error
