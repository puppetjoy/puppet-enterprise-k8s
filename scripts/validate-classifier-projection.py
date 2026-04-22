#!/usr/bin/env python3
import argparse
import json
import subprocess
import time
import uuid


ALL_NODES_GROUP_ID = "00000000-0000-4000-8000-000000000000"


def run(args, *, timeout=90):
    completed = subprocess.run(
        args,
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
        ]
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
{"login":"admin","password":"${password}","lifetime":"5m","label":"pe-k8s-conductor-classifier-proof-$(date +%s%N)"}
EOF
curl -sk --max-time 30 -H 'Content-Type: application/json' --request POST https://127.0.0.1:4433/rbac-api/v1/auth/token --data @/tmp/pe-k8s-token.json
"""
    return json.loads(exec_sh(namespace, pod, "puppetserver", script))["token"]


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


def classifier_group_exists(namespace, pod, token, group_id):
    script = r"""
set -e
cat <<'EOF' >/tmp/pe-k8s-find-group.py
import json
import sys

group_id = sys.argv[1]
payload = json.load(sys.stdin)
for group in payload:
    if not isinstance(group, dict):
        continue
    if (group.get("id") or "").strip() == group_id:
        print("found")
        raise SystemExit(0)
raise SystemExit(1)
EOF
curl -sk --max-time 30 \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  -H 'X-Authentication: __TOKEN__' \
  --request GET \
  https://127.0.0.1:4433/classifier-api/v1/groups | python3 /tmp/pe-k8s-find-group.py '__GROUP_ID__'
"""
    script = script.replace("__TOKEN__", token).replace("__GROUP_ID__", group_id)
    try:
        exec_sh(namespace, pod, "puppetserver", script)
    except subprocess.CalledProcessError:
        return False
    return True


def wait_for_group_state(namespace, pod, token, group_id, should_exist, wait_seconds):
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        exists = classifier_group_exists(namespace, pod, token, group_id)
        if exists == should_exist:
            return True
        time.sleep(5)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--writer-pod", required=True)
    parser.add_argument("--reader-pod", required=True)
    parser.add_argument("--wait-seconds", type=int, required=True)
    args = parser.parse_args()

    group_id = str(uuid.uuid4())
    group_name = f"Conductor Classifier Proof {int(time.time())}"
    writer_token = ""
    reader_token = ""

    payload = {
        "name": group_name,
        "parent": ALL_NODES_GROUP_ID,
        "environment": "production",
        "environment_trumps": False,
        "description": "Temporary classifier projection proof",
        "classes": {},
    }

    try:
        writer_token = issue_admin_token(args.namespace, args.writer_pod)
        reader_token = issue_admin_token(args.namespace, args.reader_pod)

        status = api_status(
            args.namespace,
            args.writer_pod,
            writer_token,
            "PUT",
            f"/classifier-api/v1/groups/{group_id}",
            payload,
        )
        if status not in {"200", "201"}:
            raise SystemExit(
                f"unexpected classifier create status on {args.writer_pod}: {status}"
            )
        print(
            f"[info] created temp classifier group {group_name} ({group_id}) on {args.writer_pod}",
            flush=True,
        )

        if not wait_for_group_state(
            args.namespace,
            args.writer_pod,
            writer_token,
            group_id,
            True,
            args.wait_seconds,
        ):
            raise SystemExit(
                f"classifier group {group_id} did not appear on {args.writer_pod}"
            )
        if not wait_for_group_state(
            args.namespace,
            args.reader_pod,
            reader_token,
            group_id,
            True,
            args.wait_seconds,
        ):
            raise SystemExit(
                f"classifier group {group_id} did not appear on {args.reader_pod}"
            )

        print(
            f"[ok] classifier group {group_id} is visible on {args.writer_pod} and {args.reader_pod}",
            flush=True,
        )
    finally:
        if writer_token:
            print(f"[info] deleting temp classifier group {group_id}", flush=True)
            try:
                status = api_status(
                    args.namespace,
                    args.writer_pod,
                    writer_token,
                    "DELETE",
                    f"/classifier-api/v1/groups/{group_id}",
                )
                if status not in {"204", "404"}:
                    raise SystemExit(
                        f"unexpected classifier delete status on {args.writer_pod}: {status}"
                    )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
            if reader_token:
                if not wait_for_group_state(
                    args.namespace,
                    args.reader_pod,
                    reader_token,
                    group_id,
                    False,
                    args.wait_seconds,
                ):
                    raise SystemExit(
                        f"classifier group {group_id} was not removed from {args.reader_pod}"
                    )
            print(
                f"[info] deleted temp classifier group {group_id}",
                flush=True,
            )


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired as error:
        raise SystemExit(f"timed out running kubectl exec: {error}") from error
