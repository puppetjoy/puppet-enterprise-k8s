# Conductor Architecture Direction

This repo is pivoting from a single-owner PE-on-Kubernetes baseline toward a Conductor-aligned active-active architecture.

The important boundary is simple:

- a Helm release is the intended replication domain
- separate Helm releases are independent stacks, not synchronization peers
- a release will eventually contain multiple control-plane replicas and multiple compiler replicas
- a compiler pool stays attached to the control-plane set inside its own release
- compilers do not form their own replication mesh
- shared storage is not part of the design

## Target Roles

The Conductor spec defines two PE roles:

- Worker: a PE instance that actively serves nodes
- SPOG: a PE instance used for management visibility, not for catalog service

For this repo, the near-term path is to treat a single release as one Worker-shaped stack under construction. Compiler replicas already exist as a stable in-release set. Control-plane pods now have stable per-pod identity and can participate in Fabric, but active-active control-plane service is still blocked on replicated CA and PE-owned state.

## Component Mapping

Conductor introduces four components that should become first-class parts of this project.

### Fabric

Fabric is the queue layer. It provides signed message transport, encryption, local queue ownership, and hub connectivity.

In Kubernetes terms, that means:

- one local Fabric participant per replicated workload member inside the release
- a separately managed HA Fabric hub outside the `pe` workload
- explicit onboarding and key distribution instead of PVC sharing or direct release-to-release sync

## Warden

Warden governs membership, join approval, and trust-material assembly.

It should not be bundled into the `pe` pod. It is a separate control service with authority over:

- Fabric Segment membership
- onboarding bundles
- key rotation eligibility
- compiled `ca.pem` and merged `crl.pem` distribution

Warden should be driven by release topology, not hard-coded peers. That means it needs to expand stable workload sets, notice replica-count changes, and prune stale participants automatically.

The current foundation slice models that as:

- one Fabric Segment definition
- one or more release domains inside that segment
- one or more stable workload sets inside each release domain

Today both the compiler `StatefulSet` and the `pe` control-plane `StatefulSet` fit that model. Warden can already issue onboarding bundles for those workload members, the PE chart can consume them with optional participant sidecars, and Warden can assemble a release trust bundle from control-plane CA and CRL sources. The next control-plane step is not member discovery. It is turning that trust foundation into authoritative CA behaviour and PE-state convergence between the control-plane replicas.

## Relay

Relay is the data-plane proxy between Puppet Server and PuppetDB. Its job is not to make PuppetDB shared. Its job is to preserve local PuppetDB behaviour while publishing the right writes into the Fabric.

That implies:

- local PuppetDB reads stay local
- facts replicate mesh-wide
- reports replicate to SPOGs
- full catalogs do not replicate mesh-wide
- catalog resource replication is optional and driven by cross-node PQL requirements
- catalog-serving readiness can be tied to Relay health and trust/currency checks

## Gateway

Gateway fronts orchestrator and PCP2 traffic.

That keeps PCP and orchestration in the Conductor path instead of inventing an unrelated Kubernetes-only synchronization mechanism. In this repo, Gateway placement should follow where PCP broker traffic actually terminates.

## Code Deployment

Code Manager should remain the deployment engine on each Worker.

The mesh-friendly extension is:

- an operator triggers a normal Code Manager deployment on one Worker
- a local hook publishes a signed Fabric message with the environment, immutable commit SHA, origin Worker, and deploy identifier
- peer Workers consume that intent and execute their own local Code Manager deployment for that exact revision
- each Worker publishes convergence or failure state back into Fabric
- Warden surfaces drift and can feed readiness or routing decisions for catalog-serving Workers
- attached compilers continue to receive code from their owning PE instance through normal PE file-sync

This keeps the supported PE deployment path intact while extending it across the active-active mesh.

## Non-Goals

The repo should not treat these as the HA architecture:

- direct PE-to-PE reconciliation of classifier or RBAC state
- direct PE-to-PE Code Manager deploy fanout that bypasses Fabric
- separate Helm releases acting as a fake HA control plane
- compiler-to-compiler coordination for replication
- shared RWX storage between PE instances

Conductor is the control-plane story. Code deployment should stay rooted in supported PE workflows even when Fabric starts carrying deploy intent and convergence state.

## Immediate Implementation Order

The next credible sequence is:

1. Fabric and Warden foundation
2. Relay insertion on the Puppet Server/PuppetDB path
3. Gateway insertion on the PCP/orchestrator path
4. Worker/SPOG role modelling and health-driven routing

That ordering matters because Relay and Gateway depend on Fabric and Warden for identity, trust, and transport.
