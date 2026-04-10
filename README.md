# Puppet Enterprise on Kubernetes

This repo runs Puppet Enterprise on Kubernetes by installing PE into persistent volumes and then starting PE services as Kubernetes workloads. It is the Kubernetes follow-on to the earlier container proof of concept.

## Current State

- Development and validation project, not production-ready
- Builds a PE runtime image from the official installer tarball
- Installs PE with Helm and preserves the install result on single-owner PVCs
- Runs PostgreSQL, PuppetDB, the non-compiler Puppet Server, and PE edge/API services in a single-owner `pe` pod
- Supports an optional compiler pool with per-replica non-shared PVCs, local PostgreSQL/PuppetDB, file-sync/PuppetDB-based readiness, and compiler-side PCP brokers
- Supports multiple release-scoped PE instances in one cluster, each with its own local state and optional compiler pool
- Treats the PE instance as the unit of local ownership from which active-active HA will be built
- Keeps compilers attached to one owning PE instance; compilers are not a replication mesh
- Supports optional ingress exposure, Code Manager configuration, and a validation `puppet-agent` chart with explicit certificate signing
- Still evolving toward a Conductor-aligned active-active control plane, upgrade orchestration, and hardening

## How It Relates To Traditional PE

This is not a systemd container port. PE is installed once by Kubernetes, persisted onto volumes, and then run as foreground services inside separate Kubernetes workloads. `service/pe` acts as the technical front door for agent-facing and API traffic, while an optional `service/pe-compiler` can expose the compiler pool separately.

For the deeper runtime and operator model, see:

- [docs/runtime-model.md](docs/runtime-model.md)
- [docs/conductor-architecture.md](docs/conductor-architecture.md)
- [docs/legacy-service-mapping.md](docs/legacy-service-mapping.md)
- [docs/replication-roadmap.md](docs/replication-roadmap.md)

## Repository Layout

- `image/`: PE runtime image and entrypoint scripts
- `agent-image/`: validation agent image
- `charts/`: Helm charts for PE and the validation agent
- `docs/`: runtime notes and legacy service mapping

## Prerequisites

- `podman` or another compatible OCI builder
- `helm`
- `kubectl` pointed at your cluster
- access to a container registry for the built images
- a PE installer tarball at `installers/puppet-enterprise-<version>-el-9-x86_64.tar.gz`

Depending on your environment, you may also need:

- `local/license.txt`
- `local/keys/id-control_repo.ed25519` if you enable Code Manager with an SSH deploy key

## Local Configuration

Cluster-specific settings belong in ignored local files, not in tracked defaults:

- `local/values-pe.yaml`
- `local/values-agent.yaml`

Typical overrides include:

- image repositories and tags
- storage classes
- affinity and tolerations
- ingress class, host, and TLS annotations
- PE and compiler technical hostnames
- Code Manager settings and secret references

The helper target below validates the repo-local artifact layout used by the Makefile workflow:

```bash
make check-current-state PE_VERSION=2025.9.0
```

## Build

Build the PE runtime image:

```bash
CONTAINER_ENGINE=podman \
make build-k8s-runtime \
  PE_VERSION=2025.9.0
```

By default this expects the installer tarball at:

```text
installers/puppet-enterprise-2025.9.0-el-9-x86_64.tar.gz
```

Push the runtime image if needed:

```bash
CONTAINER_ENGINE=podman \
make push-k8s-runtime \
  PE_VERSION=2025.9.0
```

Build and push the validation agent image:

```bash
CONTAINER_ENGINE=podman \
make build-k8s-agent \
  K8S_AGENT_IMAGE_VERSION=0.1.1

CONTAINER_ENGINE=podman \
make push-k8s-agent \
  K8S_AGENT_IMAGE_VERSION=0.1.1
```

Override `K8S_RUNTIME_IMAGE_NAME` or `K8S_AGENT_IMAGE_NAME` if you want to publish to a different registry or repository.

## Deploy

Render the PE chart locally:

```bash
helm template pe charts/puppet-enterprise -f local/values-pe.yaml
```

Deploy PE with the Makefile helpers:

1. Put your cluster-specific overrides in `local/values-pe.yaml`.
2. If you use Code Manager with an SSH deploy key, populate `local/keys/id-control_repo.ed25519` and run `make create-r10k-secret`.
3. If you need a PE license Secret, populate `local/license.txt`, run `make create-license-secret`, and reference that Secret from `local/values-pe.yaml`.
4. Run `make deploy-pe`.

Deploy the validation agent:

1. Put agent-specific overrides in `local/values-agent.yaml`.
2. Run `make deploy-agent`.

The validation agent chart is optional. It exists to exercise certificate issuance, catalog compilation, reporting, and Code Manager changes against a real `puppet-agent` run.
By default it can also render a signer Job that signs the test-node certificate against the in-cluster PE CA.

## What To Expect

This project is intentionally conservative right now:

- each PE instance and compiler replica owns its own non-shared PVCs
- the current `pe` workload is single-owner and single-replica oriented
- compiler capacity can scale horizontally behind `service/pe-compiler`
- centralized `puppet-code deploy` remains the code rollout entrypoint
- active-active HA work is Conductor-aligned: Fabric, Relay, Gateway, and Warden
- the repo does not yet deliver full active-active PE replication
- code rollout across Workers remains operator-initiated through Code Manager; later Fabric work may propagate deploy intent and convergence state between Workers
- charts provide generic defaults, not a ready-made cluster profile
- operators are expected to supply environment-specific values locally

If you are evaluating whether this model is a fit, start with the runtime model doc and the legacy service mapping before planning production use.
