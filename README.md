# Puppet Enterprise on Kubernetes

This repo runs Puppet Enterprise on Kubernetes by installing PE into persistent volumes and then starting PE services as Kubernetes workloads. It is the Kubernetes follow-on to the earlier container proof of concept.

## Current State

- Development and validation project, not production-ready
- Builds a PE runtime image from the official installer tarball
- Installs PE with Helm and preserves the install result on non-shared per-replica PVCs
- Runs PostgreSQL, PuppetDB, the non-compiler Puppet Server, and PE edge/API services in a stateful `pe` control-plane pod set
- Supports an optional compiler pool with per-replica non-shared PVCs, local PostgreSQL/PuppetDB, file-sync/PuppetDB-based readiness, compiler-side PCP brokers, and an internal file-sync service selector for multi-replica control planes
- Supports optional Conductor participants on the control-plane and compiler pods via Warden-issued onboarding bundles, release trust bundles, participant readiness, and a separate Fabric hub
- Supports an optional Conductor Relay sidecar that publishes local health into Fabric, records peer Relay status snapshots, replays selected PuppetDB submit-only commands, and converges replicated control-plane state such as classification, RBAC/local-auth, code-deploy intent, and managed orchestration data
- Supports multiple release-scoped PE instances in one cluster for isolated development and validation
- Uses `service/pe` as the single control-plane front door, with selector-backed stable routing when a multi-replica control plane needs one backend for console, file-sync, and orchestration traffic
- Treats one Helm release, not multiple separate releases, as the future active-active replication domain
- Keeps compilers attached to one owning PE instance; compilers are not a replication mesh
- Supports optional ingress exposure, Code Manager configuration, and a validation `puppet-agent` chart with explicit certificate signing
- Still evolving toward a Conductor-aligned active-active control plane, broader control-plane state convergence, and hardening

## How It Relates To Traditional PE

This is not a systemd container port. Each `pe` control-plane replica installs PE onto its own PVC set, then runs the PE services as foreground containers inside a StatefulSet pod. `service/pe` is the technical front door for control-plane traffic, and in a multi-replica control plane it can be pinned to one healthy `pe` replica at a time when that traffic still needs a stable backend. An optional `service/pe-compiler` can expose the compiler pool separately.

Internally, each control-plane replica installs PE against its own stable pod certname on the headless service. The shared `service/pe` address is preserved as a front door and certificate SAN, not as the replica's install identity.

When the control plane has more than one replica, `service/pe` can use
selector-backed stable routing so compiler file-sync, console, and
PCP/orchestration traffic share one healthy control-plane replica at a time.
This is a tactical safeguard for the current implementation, not a statement
that only one control-plane replica is active.

For the deeper runtime and operator model, see:

- [docs/runtime-model.md](docs/runtime-model.md)
- [docs/conductor-architecture.md](docs/conductor-architecture.md)
- [docs/legacy-service-mapping.md](docs/legacy-service-mapping.md)
- [docs/replication-roadmap.md](docs/replication-roadmap.md)

## Repository Layout

- `image/`: PE runtime image and entrypoint scripts
- `conductor-image/`: Warden, participant, and Relay image for the Conductor foundation slice
- `agent-image/`: validation agent image
- `charts/`: Helm charts for PE, the Conductor foundation, and the validation agent
- `docs/`: runtime notes and legacy service mapping

## Prerequisites

- `podman` or another compatible OCI builder
- `helm`
- `kubectl` pointed at your cluster
- access to a container registry for the built images
- a PE installer tarball available on the machine running the image build

Depending on your environment, you may also need:

- `local/license.txt`
- `local/keys/id-control_repo.ed25519` if you enable Code Manager with an SSH deploy key

## Local Configuration

Cluster-specific settings belong in ignored local files, not in tracked defaults:

- `local/values-pe.yaml`
- `local/values-agent.yaml`
- `local/values-conductor.yaml`

Typical overrides include:

- image repositories and tags
- storage classes
- affinity and tolerations
- `controlPlane.resources.*` and `compilers.resources.*` container resources
- ingress class, host, and TLS annotations
- PE and compiler technical hostnames
- Code Manager settings and secret references

The helper target below validates the repo-local artifact layout used by the
default Makefile workflow:

```bash
make check-current-state PE_VERSION=2025.9.0
```

## Build

Build the PE runtime image:

```bash
CONTAINER_ENGINE=podman \
make build-k8s-runtime \
  PE_VERSION=2025.9.0 \
  PE_INSTALLER_TAR_PATH=/absolute/path/to/puppet-enterprise-2025.9.0-el-9-x86_64.tar.gz
```

`PE_INSTALLER_TAR_PATH` is the real build input. The Makefile stages that
archive into the image build context before invoking `podman build`.

If you do not set `PE_INSTALLER_TAR_PATH`, the Makefile defaults to the
repo-local convention:

```text
installers/puppet-enterprise-2025.9.0-el-9-x86_64.tar.gz
```

That path is a convenience for local development, not a required repository
layout for user-supplied installer artifacts.

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

Build and push the Conductor image:

```bash
CONTAINER_ENGINE=podman \
make build-conductor \
  CONDUCTOR_IMAGE_VERSION=0.1.0

CONTAINER_ENGINE=podman \
make push-conductor \
  CONDUCTOR_IMAGE_VERSION=0.1.0
```

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

Deploy the Conductor foundation slice:

1. Put Conductor-specific overrides in `local/values-conductor.yaml`.
2. Run `make deploy-conductor`.

If you want the PE and compiler pods to join Fabric, also set matching `conductor.*`
values in `local/values-pe.yaml` and redeploy the PE chart.

The validation agent chart is optional. It exists to exercise certificate issuance, catalog compilation, reporting, and Code Manager changes against a real `puppet-agent` run.
By default it can also render a signer Job that signs the test-node certificate against the in-cluster PE CA.

## What To Expect

This project is intentionally conservative right now:

- each PE instance and compiler replica owns its own non-shared PVCs
- the control plane now has stable per-replica identity and storage, but active-active control-plane synchronization is still in development
- the current Conductor foundation slice can onboard the `pe` control-plane pods and attached compiler pods into Fabric, then assemble and distribute a release trust bundle
- when Conductor is enabled, pod readiness can follow onboarding, Fabric connectivity, and trust-bundle installation so `service/pe` and `service/pe-compiler` stop routing to stale or disconnected participants
- when `conductor.relay.enabled=true`, control-plane and compiler pods also publish Relay status into Fabric, can gate readiness on participant trust plus local PuppetDB health, and can replay selected PuppetDB command traffic through Fabric
- when `conductor.relay.classifierSync.enabled=true`, control-plane relays replicate the managed user-visible classifier domain under `All Nodes`, including the `All Environments` subtree and `PE Patch Management`, while leaving PE-owned local infrastructure groups like `PE Infrastructure` out of the sync domain
- when `conductor.gateway.enabled=true`, control-plane pods front PCP and orchestration traffic through a Gateway sidecar that proxies local `8142/8143` listeners, publishes Gateway status into Fabric, and removes disconnected or unhealthy control-plane replicas from service routing
- when the control plane has more than one replica, `service/pe` now uses selector-backed stable routing for PCP broker and orchestration traffic instead of exposing a separate sticky service; live validation now covers task execution, plan execution, and selector failover between `pe` replicas
- when `conductor.relay.rbacSync.enabled=true`, control-plane relays replicate PE RBAC and local-auth managed state through Fabric, share console token-signing and SAML material, and intentionally treat per-replica login activity such as `last_login` as non-authoritative
- compiler capacity can scale horizontally behind `service/pe-compiler`
- centralized `puppet-code deploy` remains the code rollout entrypoint
- separate Helm releases are independent sandboxes, not synchronization peers
- active-active HA work is Conductor-aligned: Fabric, Relay, Gateway, and Warden
- when the control plane has more than one replica, the chart now deliberately routes the web console, compiler file-sync, and orchestration traffic through selector-backed `service/pe` instead of splitting those same surfaces onto a second control-plane service; CA, classification, code-deploy intent, and RBAC/local-auth state still converge underneath that stable-backend boundary
- the repo does not yet deliver full active-active PE replication
- the current Relay implementation now captures selected PuppetDB submit-only commands, replays facts and reports to the control-plane role, and intentionally keeps full catalogs local-only
- classifier HA currently covers that filtered managed domain rather than a dedicated user subtree; PE-owned local classifier groups still remain locally owned on each control-plane replica
- the current Gateway and Relay implementation now covers sticky PCP/orchestration routing, managed orchestration job-state convergence, and broker failover between control-plane replicas; `pe-inventory` now appears to back saved connection inventory rather than live PCP broker presence, so broader PCP mediation is still ahead
- code rollout across Workers remains operator-initiated through Code Manager; later Fabric work may propagate deploy intent and convergence state between Workers
- charts provide generic defaults, not a ready-made cluster profile
- operators are expected to supply environment-specific values locally

If you are evaluating whether this model is a fit, start with the runtime model doc and the legacy service mapping before planning production use.
