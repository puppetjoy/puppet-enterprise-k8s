# Conductor Architecture Direction

This repo is pivoting from a single-owner PE-on-Kubernetes baseline toward a Conductor-aligned active-active architecture.

The important boundary is simple:

- a PE instance is the unit of control-plane ownership
- a compiler pool stays attached to one PE instance
- compilers do not form their own replication mesh
- shared storage is not part of the design

## Target Roles

The Conductor spec defines two PE roles:

- Worker: a PE instance that actively serves nodes
- SPOG: a PE instance used for management visibility, not for catalog service

For this repo, the near-term path is to make the PE instance the thing that can become a Worker. Compiler replicas remain the node-serving edge attached to that PE instance until the Conductor data paths are fully in place.

## Component Mapping

Conductor introduces four components that should become first-class parts of this project.

### Fabric

Fabric is the queue layer. It provides signed message transport, encryption, local queue ownership, and hub connectivity.

In Kubernetes terms, that means:

- one local Fabric participant per PE instance
- a separately managed HA Fabric hub outside the `pe` workload
- explicit onboarding and key distribution instead of PVC sharing or direct PE-to-PE sync

## Warden

Warden governs membership, join approval, and trust-material assembly.

It should not be bundled into the `pe` pod. It is a separate control service with authority over:

- Fabric Segment membership
- onboarding bundles
- key rotation eligibility
- compiled `ca.pem` and merged `crl.pem` distribution

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
