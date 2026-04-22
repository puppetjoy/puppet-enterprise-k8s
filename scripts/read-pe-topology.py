#!/usr/bin/env python3

import argparse
import sys


def load_yaml(path):
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "PyYAML is required to read Helm values files. "
            "Install python3-yaml or pip install pyyaml."
        ) from exc

    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"expected a YAML mapping in {path}")
    return data


def get_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
    return bool(value)


def get_int(value, default=0):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"expected an integer-compatible value, got {value!r}") from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("values_file")
    parser.add_argument(
        "--format",
        choices=("shell", "json"),
        default="shell",
    )
    args = parser.parse_args()

    values = load_yaml(args.values_file)
    control_plane = values.get("controlPlane") or {}
    compilers = values.get("compilers") or {}

    control_plane_replica_count = get_int(control_plane.get("replicaCount"), 1)
    compilers_enabled = get_bool(compilers.get("enabled"), False)
    configured_compiler_replica_count = get_int(compilers.get("replicaCount"), 0)
    compiler_replica_count = configured_compiler_replica_count if compilers_enabled else 0

    topology = {
        "controlPlaneReplicaCount": control_plane_replica_count,
        "compilerReplicaCount": compiler_replica_count,
        "compilerPoolEnabled": compiler_replica_count > 0,
        "replicatedControlPlaneEnabled": control_plane_replica_count > 1,
        "foundationRequired": (
            control_plane_replica_count > 1 and compiler_replica_count > 0
        ),
    }

    if args.format == "json":
        import json

        json.dump(topology, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return

    for key, value in topology.items():
        shell_key = []
        for char in key:
            if char.isupper():
                shell_key.append("_")
                shell_key.append(char)
            else:
                shell_key.append(char.upper())
        env_name = f"PE_TOPOLOGY_{''.join(shell_key)}"
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        print(f"{env_name}={rendered}")


if __name__ == "__main__":
    main()
