# Conductor Architecture Direction

This repo uses Conductor to build a replicated PE control plane on Kubernetes
without shared storage.

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

In this repo those are logical traffic roles, not Kubernetes deployment shapes
that we must force into the chart. The current Kubernetes-first goal is a
single release with multiple equivalent `pe` control-plane replicas behind
`service/pe`, plus a compiler pool behind `service/pe-compiler`.

That means:

- `service/pe` remains the control-plane front door
- one selected `service/pe` backend at a time is the current HA contract
- permanent pinning of compilers or clients to one specific `pe` replica is not the design goal
- temporary routing constraints are acceptable only as tactical safety measures while a specific surface is not yet replica-safe
- SPOG-only replicas are optional future topology, not a required milestone for this repo

Control-plane pods now have stable per-pod identity and can participate in
Fabric, but correctness still depends on explicit shared-state and failover
boundaries rather than arbitrary pooling.

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

Today both the compiler `StatefulSet` and the `pe` control-plane `StatefulSet`
fit that model. Warden can already issue onboarding bundles for those workload
members, the PE chart can consume them with optional participant sidecars,
Warden can assemble a release trust bundle from control-plane CA and CRL
sources, and participant readiness can feed service routing.

## Relay

Relay is the data-plane proxy between Puppet Server and PuppetDB. Its job is not to make PuppetDB shared. Its job is to preserve local PuppetDB behaviour while publishing the right writes into the Fabric.

That implies:

- local PuppetDB reads stay local
- facts replicate mesh-wide
- reports replicate to SPOGs
- full catalogs do not replicate mesh-wide
- catalog resource replication is optional and driven by cross-node PQL requirements
- catalog-serving readiness can be tied to Relay health and trust/currency checks

The current repo now has the first Relay slice wired into the runtime model:

- an optional `conductor-relay` sidecar can run beside the control-plane and compiler service containers
- Relay reuses the pod's Warden-issued onboarding bundle to join Fabric on its own queue
- Relay polls local PuppetDB status, publishes that health view into Fabric, and stores fresh peer Relay snapshots locally
- Puppet Server keeps `server_urls` pointed at local PuppetDB while `submit_only_server_urls` is patched to the pod-local Relay command proxy
- Relay captures selected submit-only PuppetDB commands from local Puppet Server, publishes them into Fabric, and replays them to the `control-plane` role
- facts, reports, and deactivate-node commands can now traverse Fabric; full catalogs still remain local-only by default
- Relay readiness is tied to participant trust readiness plus local PuppetDB health

This is intentionally not the whole Relay design yet. The current implementation now has a working write path for selected PuppetDB commands, but authoritative control-plane convergence and any optional catalog-resource replication are still ahead.

## Gateway

Gateway fronts orchestrator and PCP2 traffic.

That keeps PCP and orchestration in the Conductor path instead of inventing an unrelated Kubernetes-only synchronization mechanism. In this repo, Gateway placement should follow where PCP broker traffic actually terminates.

The current repo now has the first Gateway slice wired into the control-plane runtime:

- an optional `conductor-gateway` sidecar can run beside the `pe` control-plane service containers
- `service/pe` and `pe-headless` can route `8142` and `8143` to Gateway listener ports instead of targeting `orchestration-services` directly
- Gateway proxies raw TCP traffic to the pod-local orchestration service, so PCP broker TLS and orchestration HTTPS stay intact without a second TLS termination layer
- Gateway reuses the pod's Warden-issued onboarding bundle to join Fabric on its own queue, publishes local Gateway health into Fabric, and stores fresh peer Gateway snapshots locally
- Gateway readiness is tied to participant trust readiness plus local PCP broker and orchestration health, which makes `service/pe` drain stale or isolated control-plane replicas

This is still not the whole Gateway design. The current implementation establishes service ownership, trust-aware readiness, selector-backed routing on `service/pe`, and validated task/plan failover on the PCP/orchestration ingress path. The current evidence suggests that `pe-inventory` backs saved connection inventory rather than live PCP broker session state, so an empty `pe-inventory` database during certname-driven PCP execution is not itself a replication failure. Broader PCP semantics still are not mediated through Fabric.

## Code Deployment

Code Manager should remain the deployment engine on each Worker.

The mesh-friendly extension is:

- an operator triggers a normal Code Manager deployment on one Worker
- a local post-environment hook publishes a signed Fabric message with the environment, deploy signature, origin Worker, deploy identifier, and origin file-sync metadata
- peer Workers consume that intent and execute their own local Code Manager deployment
- each Worker publishes convergence or failure state back into Fabric
- Warden surfaces drift and can feed readiness or routing decisions for catalog-serving Workers
- attached compilers continue to receive code through normal PE file-sync from the release-local control-plane service path

This keeps the supported PE deployment path intact while extending it across
the replicated control-plane mesh.

The current implementation uses the PE Code Manager deploy signature as the
cross-Worker convergence token. That value is stable across Workers for the
same deployment, while the local file-sync commit identifiers observed on each
Worker are not. Relay therefore records the origin Worker file-sync metadata
for operator context, but it gates readiness on deploy-signature convergence.

The current runtime also keeps compiler file-sync fetches on one selected
`service/pe` backend at a time. That is a tactical compiler-facing safeguard
while PE file-sync object ownership is still instance-local. It is not a
permanent topology goal or a SPOG requirement.

The current flow is:

- Puppet Server writes a Code Manager post-environment hook at startup when relay code deployment is enabled
- the local relay hook publishes `ConductorRelayCodeDeployIntent` into Fabric after a successful local deploy
- remote relays mark themselves pending, drain from readiness, replay the deploy locally through the Code Manager API, and suppress their local hook so the replay does not loop
- relays only become ready again when their local deploy signature matches the desired signature

## Shared Classification

Shared classification now uses the same Conductor shared-state model as the
other migrated control-plane domains.

The current implementation no longer exposes a Conductor-specific classifier
root to users. Instead, Relay projects a filtered managed domain from the live
classifier tree rooted at `All Nodes`.

That managed domain currently:

- keeps `All Nodes` as the stable top-level anchor
- treats `All Environments` and installer-created user-facing containers such as `PE Patch Management` as semantic anchors whose local IDs may differ per replica
- preserves the local IDs of those semantic anchors while synchronizing their contents and descendants
- excludes PE-owned local infrastructure roots such as `PE Infrastructure`
- ignores and retires the legacy `Conductor Shared Classification` root when it is still present and empty

Relay now writes that translated managed domain into Cassandra-backed shared
state and uses Fabric for convergence signals. Peer replicas rebuild their
local classifier state from that shared graph while preserving their local
anchor IDs.

That means:

- users can create ordinary node-group hierarchies directly under `All Nodes`
- the `All Environments` subtree can converge even when installer-created group IDs differ between `pe` replicas
- installer-created but user-managed containers like `PE Patch Management` remain inside the replicated domain
- PE-owned infrastructure groups remain local to each control-plane replica
- the mechanism stays Conductor-aligned because shared state is carried
  through Conductor rather than direct PE-to-PE API fanout
- RBAC, login sessions, and persisted orchestration state now follow the same
  shared-state pattern
- console sessions and other remaining console-backed behaviors still depend on
  the selected `service/pe` backend

## Shared State Backend

The current repo direction is Cassandra-backed shared state for shared
control-plane domains rather than peer PostgreSQL replay.

The intended target is:

- Fabric for transport and convergence signals
- Cassandra for durable shared state
- equivalent `pe` replicas as execution frontends behind `service/pe`

This keeps the current Kubernetes deployment shape while removing the most
fragile part of the current design: rewriting managed database tables from one
`pe` replica into another.

The architectural commitment is shared state in Cassandra, not a new named
middle-tier service.

See [Shared State Backend](shared-state-backend.md) for the current shared-state
model.

## Shared RBAC and Local Auth

RBAC and local-auth state now use the same Cassandra-backed shared-state model
as the other migrated control-plane domains.

The current implementation:

- shares console token-signing and optional SAML material across `pe` replicas
  so locally issued auth tokens can validate on peers
- projects the managed RBAC database domain through Conductor and rehydrates it
  into local PE PostgreSQL from Cassandra-backed authority
- excludes operator and automation token labels with reserved prefixes such as `pe-k8s-conductor-` so local maintenance tokens are not treated as replicated user state
- intentionally normalizes ephemeral per-replica activity fields such as `subjects.last_login` and token `last_active` so ordinary authentication traffic does not create readiness churn

That means:

- local-auth users, roles, role bindings, and normal user tokens can converge between `pe` replicas
- a token issued on one control-plane replica can become valid on its peer without shared storage
- the replicated RBAC domain remains authoritative enough for readiness while leaving replica-local operator diagnostics outside the convergence token
- browser traffic still runs through one selected `service/pe` backend at a
  time even though the underlying auth state is shared

With the Cassandra backend enabled for `rbacSync`, `rbacTokenSync`, and
login-session handoff, shared auth-related state can now live on a
Cassandra-backed Conductor domain while local PE service behaviour stays
intact.

Login-session handoff, the managed RBAC graph, and normal RBAC tokens now all
follow that model. Local RBAC PostgreSQL remains the execution-local
projection, not the shared authority.

## Shared Orchestration State

Managed orchestration state now follows the same Cassandra-backed shared-state
pattern.

The current implementation:

- can publish persisted `pe-inventory` and managed `pe-orchestrator` state into Cassandra as durable shared control-plane state
- uses Fabric for convergence signals while peer `pe` replicas rehydrate their own local `pe-inventory` and `pe-orchestrator` databases from Cassandra-backed state
- excludes local-only discovered PCP connections from the shared
  `pe-inventory` projection so live broker presence is not replayed as durable
  state
- still reserves per-replica sequence residues so local PE databases stay safe for new inserts after rehydration
- routes compiler PCP brokers, Bolt/orchestrator clients, console traffic, and compiler file-sync through selector-backed `service/pe` so one healthy control-plane replica owns those stable-backend surfaces at a time
- publishes per-pod front-door eligibility and blocker annotations from Relay so `service/pe` promotion is driven by convergence state instead of Pod readiness alone
- keeps Gateway as the transport and health boundary on `8142` and `8143`
- treats `pe-inventory` as persisted connection inventory for saved targets and transport parameters, not as the source of truth for live PCP-connected certnames

That means:

- task and plan execution can survive control-plane failover without shared storage
- orchestration job and plan state can reconverge after a replica returns
- `service/pe` currently uses selector-backed stable routing for those surfaces until they are replica-safe
- an empty `pe-inventory` database during certname-driven PCP execution is currently expected
- broader PCP mediation and any remaining inventory surfaces beyond saved
  connection records remain open follow-up
- repo helpers now expose that state directly: `scripts/pe-frontdoor-status.sh` shows the current backend and blockers, and `scripts/validate-pe-failover.sh` exercises a live cutover

Persisted saved inventory and persisted orchestration job state are now on that
path. Live discovered PCP connections remain intentionally local.

## Non-Goals

The repo should not treat these as the HA architecture:

- direct PE-to-PE reconciliation of classifier or RBAC state
- direct PE-to-PE Code Manager deploy fanout that bypasses Fabric
- separate Helm releases acting as a fake HA control plane
- compiler-to-compiler coordination for replication
- shared RWX storage between PE instances

Conductor is the control-plane story. Code deployment should stay rooted in supported PE workflows even when Fabric starts carrying deploy intent and convergence state.

## Current Follow-On Work

The main follow-on work is now:

1. Cassandra operational hardening, including backup and restore proof
2. cleanup of any remaining code and docs that still assume the old peer
   PostgreSQL model
3. reduction of any remaining selected-backend constraints only where that
   produces a clearer HA story
