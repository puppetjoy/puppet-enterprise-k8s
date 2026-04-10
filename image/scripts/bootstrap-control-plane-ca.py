#!/usr/bin/env python3

import base64
import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def log(message):
    print(f"[pe-k8s-control-plane-ca] {message}", flush=True)


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def b64encode_text(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def b64decode_text(value):
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def split_pem_blocks(value, begin_marker, end_marker):
    blocks = []
    current = []
    in_block = False

    for line in value.splitlines(keepends=True):
        if line.startswith(begin_marker):
            current = [line]
            in_block = True
            continue

        if not in_block:
            continue

        current.append(line)
        if line.startswith(end_marker):
            blocks.append("".join(current))
            current = []
            in_block = False

    return blocks


def write_text(path, content):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content, encoding="utf-8")


def run_command(args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text(encoding="utf-8").strip()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

    def request(self, method, path, payload=None, expected=None):
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}{path}", method=method, data=data)
        for key, value in self.headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=30) as response:
                body = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            status = error.code
        if expected and status not in expected:
            raise RuntimeError(f"Kubernetes API {method} {path} returned {status}: {body}")
        return status, json.loads(body) if body else None

    def get_secret(self, name, namespace=None):
        namespace = namespace or self.namespace
        status, data = self.request(
            "GET",
            f"/api/v1/namespaces/{namespace}/secrets/{name}",
            expected={200, 404},
        )
        return data if status == 200 else None

    def upsert_secret(self, name, string_data, labels=None, annotations=None, namespace=None):
        namespace = namespace or self.namespace
        desired = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels or {},
                "annotations": annotations or {},
            },
            "type": "Opaque",
            "data": {key: b64encode_text(value) for key, value in string_data.items()},
        }
        existing = self.get_secret(name, namespace=namespace)
        if existing is not None:
            desired["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
            self.request(
                "PUT",
                f"/api/v1/namespaces/{namespace}/secrets/{name}",
                payload=desired,
                expected={200},
            )
            return
        self.request(
            "POST",
            f"/api/v1/namespaces/{namespace}/secrets",
            payload=desired,
            expected={201},
        )


def control_plane_pod_name(statefulset_name, ordinal):
    return f"{statefulset_name}-{ordinal}"


def control_plane_certname(pod_name, headless_service, namespace):
    return f"{pod_name}.{headless_service}.{namespace}.svc.cluster.local"


def control_plane_ca_secret_name(pod_name):
    return f"{pod_name}-ca"


def control_plane_ca_bundle_secret_name(default_name):
    return os.environ.get("PE_CONTROL_PLANE_CA_BUNDLE_SECRET_NAME", "").strip() or default_name


def build_root_openssl_config(common_name):
    return f"""[ ca ]
default_ca = CA_default
[ CA_default ]
dir = .
new_certs_dir = $dir/certs
database = $dir/index.txt
serial = $dir/serial
crlnumber = $dir/crlnumber
default_md = sha256
default_crl_days = 3650
policy = policy_loose
certificate = $dir/root.crt
private_key = $dir/root.key
copy_extensions = copy
[ policy_loose ]
commonName = supplied
[ req ]
default_bits = 4096
distinguished_name = req_dn
x509_extensions = v3_ca
prompt = no
[ req_dn ]
CN = {common_name}
[ v3_ca ]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical, CA:true
keyUsage = critical, digitalSignature, cRLSign, keyCertSign
"""


def build_intermediate_ext_config(common_name):
    return f"""[ req ]
default_bits = 4096
distinguished_name = req_dn
prompt = no
[ req_dn ]
CN = {common_name}
[ v3_intermediate_ca ]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
basicConstraints = critical, CA:true, pathlen:0
keyUsage = critical, digitalSignature, cRLSign, keyCertSign
"""


def generate_empty_crl(ca_cert, ca_key, output_path):
    with tempfile.TemporaryDirectory(prefix="pe-k8s-ca-crl-") as workdir:
        work = Path(workdir)
        (work / "certs").mkdir(parents=True, exist_ok=True)
        (work / "index.txt").write_text("", encoding="utf-8")
        (work / "serial").write_text("1000\n", encoding="utf-8")
        (work / "crlnumber").write_text("1000\n", encoding="utf-8")
        (work / "ca.crt").write_text(ca_cert, encoding="utf-8")
        (work / "ca.key").write_text(ca_key, encoding="utf-8")
        write_text(
            work / "openssl.cnf",
            """[ ca ]
default_ca = CA_default
[ CA_default ]
dir = .
new_certs_dir = $dir/certs
database = $dir/index.txt
serial = $dir/serial
crlnumber = $dir/crlnumber
default_md = sha256
default_crl_days = 3650
policy = policy_loose
certificate = $dir/ca.crt
private_key = $dir/ca.key
[ policy_loose ]
commonName = supplied
""",
        )
        run_command(["openssl", "ca", "-config", "openssl.cnf", "-gencrl", "-out", str(output_path)], cwd=workdir)


def generate_root_material(common_name, duration_days, key_algorithm, key_size, initial_serial):
    if key_algorithm.upper() != "RSA":
        raise RuntimeError(f"unsupported key algorithm {key_algorithm!r}; only RSA is currently supported")

    with tempfile.TemporaryDirectory(prefix="pe-k8s-root-ca-") as workdir:
        work = Path(workdir)
        (work / "certs").mkdir(parents=True, exist_ok=True)
        (work / "index.txt").write_text("", encoding="utf-8")
        (work / "serial").write_text(f"{initial_serial}\n", encoding="utf-8")
        (work / "crlnumber").write_text("1000\n", encoding="utf-8")
        write_text(work / "openssl.cnf", build_root_openssl_config(common_name))
        run_command(
            [
                "openssl",
                "req",
                "-config",
                "openssl.cnf",
                "-new",
                "-x509",
                "-days",
                str(duration_days),
                "-newkey",
                f"rsa:{key_size}",
                "-keyout",
                "root.key",
                "-out",
                "root.crt",
                "-nodes",
            ],
            cwd=workdir,
        )
        run_command(["openssl", "ca", "-config", "openssl.cnf", "-gencrl", "-out", "root.crl"], cwd=workdir)
        return {
            "root.crt": (work / "root.crt").read_text(encoding="utf-8"),
            "root.key": (work / "root.key").read_text(encoding="utf-8"),
            "root.crl": (work / "root.crl").read_text(encoding="utf-8"),
            "nextSerial": f"{initial_serial}\n",
        }


def generate_intermediate_material(root_cert, root_key, root_crl, common_name, duration_days, key_size, serial_number):
    with tempfile.TemporaryDirectory(prefix="pe-k8s-intermediate-ca-") as workdir:
        work = Path(workdir)
        write_text(work / "root.crt", root_cert)
        write_text(work / "root.key", root_key)
        write_text(work / "intermediate.cnf", build_intermediate_ext_config(common_name))
        run_command(
            [
                "openssl",
                "req",
                "-config",
                "intermediate.cnf",
                "-new",
                "-newkey",
                f"rsa:{key_size}",
                "-keyout",
                "intermediate.key",
                "-out",
                "intermediate.csr",
                "-nodes",
            ],
            cwd=workdir,
        )
        run_command(
            [
                "openssl",
                "x509",
                "-req",
                "-days",
                str(duration_days),
                "-in",
                "intermediate.csr",
                "-CA",
                "root.crt",
                "-CAkey",
                "root.key",
                "-set_serial",
                f"0x{serial_number:X}",
                "-extensions",
                "v3_intermediate_ca",
                "-extfile",
                "intermediate.cnf",
                "-out",
                "intermediate.crt",
            ],
            cwd=workdir,
        )
        intermediate_cert = (work / "intermediate.crt").read_text(encoding="utf-8")
        intermediate_key = (work / "intermediate.key").read_text(encoding="utf-8")
        generate_empty_crl(intermediate_cert, intermediate_key, work / "intermediate.crl")
        crl_chain = (work / "intermediate.crl").read_text(encoding="utf-8") + root_crl
        return {
            "tls.crt": intermediate_cert,
            "tls.key": intermediate_key,
            "ca.crt": root_cert,
            "crl.pem": crl_chain,
        }


def main():
    namespace = os.environ["PE_CONTROL_PLANE_NAMESPACE"].strip()
    statefulset_name = os.environ["PE_CONTROL_PLANE_STATEFULSET_NAME"].strip()
    headless_service = os.environ["PE_CONTROL_PLANE_HEADLESS_SERVICE"].strip()
    replica_count = env_int("PE_CONTROL_PLANE_REPLICA_COUNT", 1)
    root_secret_name = os.environ["PE_CONTROL_PLANE_CA_ROOT_SECRET_NAME"].strip()
    bundle_secret_name = control_plane_ca_bundle_secret_name(f"{statefulset_name}-control-plane-ca-bundle")
    root_common_name = os.environ.get("PE_CONTROL_PLANE_CA_ROOT_COMMON_NAME", "").strip() or f"Puppet Root CA: {statefulset_name}"
    duration_days = env_int("PE_CONTROL_PLANE_CA_DURATION_DAYS", 3650)
    intermediate_duration_days = env_int("PE_CONTROL_PLANE_CA_INTERMEDIATE_DURATION_DAYS", 3650)
    key_algorithm = os.environ.get("PE_CONTROL_PLANE_CA_KEY_ALGORITHM", "RSA").strip() or "RSA"
    key_size = env_int("PE_CONTROL_PLANE_CA_KEY_SIZE", 4096)
    initial_serial = env_int("PE_CONTROL_PLANE_CA_INITIAL_SERIAL", 1000)

    k8s = K8sApi(namespace)

    root_secret = k8s.get_secret(root_secret_name, namespace=namespace)
    if root_secret is None:
        log(f"Generating shared root CA Secret {namespace}/{root_secret_name}")
        root_material = generate_root_material(root_common_name, duration_days, key_algorithm, key_size, initial_serial)
        k8s.upsert_secret(
            root_secret_name,
            root_material,
            labels={"pe-k8s.puppet.com/control-plane-ca": "root"},
            annotations={"pe-k8s.puppet.com/statefulset": statefulset_name},
            namespace=namespace,
        )
    else:
        data = root_secret.get("data", {})
        root_material = {
            "root.crt": b64decode_text(data["root.crt"]),
            "root.key": b64decode_text(data["root.key"]),
            "root.crl": b64decode_text(data["root.crl"]),
            "nextSerial": b64decode_text(data.get("nextSerial", b64encode_text(f"{initial_serial}\n"))),
        }

    next_serial = int(root_material["nextSerial"].strip() or initial_serial)
    root_updated = False

    for ordinal in range(replica_count):
        pod_name = control_plane_pod_name(statefulset_name, ordinal)
        certname = control_plane_certname(pod_name, headless_service, namespace)
        secret_name = control_plane_ca_secret_name(pod_name)
        if k8s.get_secret(secret_name, namespace=namespace) is not None:
            continue

        log(f"Generating intermediate CA Secret {namespace}/{secret_name} for {certname}")
        intermediate = generate_intermediate_material(
            root_material["root.crt"],
            root_material["root.key"],
            root_material["root.crl"],
            f"Puppet Enterprise CA: {pod_name}",
            intermediate_duration_days,
            key_size,
            next_serial,
        )
        k8s.upsert_secret(
            secret_name,
            intermediate,
            labels={"pe-k8s.puppet.com/control-plane-ca": "intermediate"},
            annotations={
                "pe-k8s.puppet.com/statefulset": statefulset_name,
                "pe-k8s.puppet.com/pod-name": pod_name,
                "pe-k8s.puppet.com/certname": certname,
            },
            namespace=namespace,
        )
        next_serial += 1
        root_updated = True

    if root_updated:
        root_material["nextSerial"] = f"{next_serial}\n"
        k8s.upsert_secret(
            root_secret_name,
            root_material,
            labels={"pe-k8s.puppet.com/control-plane-ca": "root"},
            annotations={"pe-k8s.puppet.com/statefulset": statefulset_name},
            namespace=namespace,
        )

    intermediate_certs = []
    intermediate_crls = []
    for ordinal in range(replica_count):
        pod_name = control_plane_pod_name(statefulset_name, ordinal)
        secret_name = control_plane_ca_secret_name(pod_name)
        secret = k8s.get_secret(secret_name, namespace=namespace)
        if secret is None:
            raise RuntimeError(f"control-plane CA Secret {namespace}/{secret_name} is missing")
        data = secret.get("data", {})
        intermediate_certs.append(b64decode_text(data["tls.crt"]))
        crl_chain = b64decode_text(data["crl.pem"])
        crls = split_pem_blocks(crl_chain, "-----BEGIN X509 CRL-----", "-----END X509 CRL-----")
        if not crls:
            raise RuntimeError(f"control-plane CA Secret {namespace}/{secret_name} is missing X509 CRLs")
        intermediate_crls.append(crls[0])

    aggregate_bundle = "".join(intermediate_certs) + root_material["root.crt"]
    aggregate_crl_chain = "".join(intermediate_crls) + root_material["root.crl"]
    k8s.upsert_secret(
        bundle_secret_name,
        {
            "ca.pem": aggregate_bundle,
            "ca.crt": aggregate_bundle,
            "intermediates.pem": "".join(intermediate_certs),
            "crl.pem": aggregate_crl_chain,
            "intermediate-crls.pem": "".join(intermediate_crls),
            "root.crt": root_material["root.crt"],
            "root.crl": root_material["root.crl"],
        },
        labels={"pe-k8s.puppet.com/control-plane-ca": "bundle"},
        annotations={"pe-k8s.puppet.com/statefulset": statefulset_name},
        namespace=namespace,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
