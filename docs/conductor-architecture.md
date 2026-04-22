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

In this repo those are logical traffic roles, not Kubernetes deployment shapes
that we must force into the chart. The current Kubernetes-first goal is still a
single release with multiple equivalent `pe` control-plane replicas behind
`service/pe`, plus a compiler pool behind `service/pe-compiler`.

That means:

- `service/pe` remains the target pooled front door for control-plane traffic
- permanent pinning of compilers or clients to one specific `pe` replica is not the design goal
- temporary routing constraints are acceptable only as tactical safety measures while a specific surface is not yet replica-safe
- SPOG-only replicas are optional future topology, not a required milestone for this repo

Control-plane pods now have stable per-pod identity and can participate in
Fabric, but active-active control-plane service is still blocked on replicated
CA and PE-owned state.

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

Today both the compiler `StatefulSet` and the `pe` control-plane `StatefulSet` fit that model. Warden can already issue onboarding bundles for those workload members, the PE chart can consume them with optional participant sidecars, Warden can assemble a release trust bundle from control-plane CA and CRL sources, and participant readiness can feed service routing. The next control-plane step is not member discovery. It is turning that trust foundation into authoritative CA behaviour and PE-state convergence between the control-plane replicas.

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

This keeps the supported PE deployment path intact while extending it across the active-active mesh.

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

The first PE-owned control-plane state now carried through Fabric is shared
classification.

The current implementation no longer exposes a Conductor-specific classifier
root to users. Instead, Relay projects a filtered managed domain from the live
classifier tree rooted at `All Nodes`.

That managed domain currently:

- keeps `All Nodes` as the stable top-level anchor
- treats `All Environments` and installer-created user-facing containers such as `PE Patch Management` as semantic anchors whose local IDs may differ per replica
- preserves the local IDs of those semantic anchors while synchronizing their contents and descendants
- excludes PE-owned local infrastructure roots such as `PE Infrastructure`
- ignores and retires the legacy `Conductor Shared Classification` root when it is still present and empty

Relay publishes that translated managed domain into Fabric and replays create,
update, and delete operations on peer replicas against their local anchor IDs.
That means:

- users can create ordinary node-group hierarchies directly under `All Nodes`
- the `All Environments` subtree can converge even when installer-created group IDs differ between `pe` replicas
- installer-created but user-managed containers like `PE Patch Management` remain inside the replicated domain
- PE-owned infrastructure groups remain local to each control-plane replica
- the mechanism stays Conductor-aligned because state moves through Fabric rather than through direct PE-to-PE API fanout
- RBAC and local-auth managed state now converge through Fabric as part of the same PE-owned control-plane domain
- console sessions and other remaining console-backed writes are still ahead

## Shared State Backend

The current repo direction is to retire peer PostgreSQL replay for shared
control-plane domains and replace it with a Conductor-owned shared backend.

The intended target is:

- Fabric for transport and convergence signals
- Cassandra for durable shared state
- equivalent `pe` replicas as execution frontends behind `service/pe`

This keeps the current Kubernetes deployment shape while removing the most
fragile part of the current design: rewriting managed database tables from one
`pe` replica into another.

The architectural commitment is shared state in Cassandra, not a new named
middle-tier service.

See [Shared State Backend](shared-state-backend.md) for the migration target
and slice order.

## Shared RBAC and Local Auth

The next PE-owned control-plane slice now carried through Fabric is RBAC and
local-auth state.

The current implementation:

- shares console token-signing and optional SAML material across `pe` replicas so locally issued auth tokens can validate on peers
- projects the managed RBAC database domain through Fabric and replays it onto peer control-plane replicas
- excludes operator and automation token labels with reserved prefixes such as `pe-k8s-conductor-` so local maintenance tokens are not treated as replicated user state
- intentionally normalizes ephemeral per-replica activity fields such as `subjects.last_login` and token `last_active` so ordinary authentication traffic does not create readiness churn

That means:

- local-auth users, roles, role bindings, and normal user tokens can converge between `pe` replicas
- a token issued on one control-plane replica can become valid on its peer without shared storage
- the replicated RBAC domain remains authoritative enough for readiness while leaving replica-local operator diagnostics outside the convergence token
- web console sessions remain local to the selected `service/pe` backend and are intentionally kept outside the replicated domain so browser traffic can stay consistent even while replicated state converges asynchronously

This is now considered transitional. The target is to move shared auth-related
state off peer PostgreSQL replay and onto a Cassandra-backed Conductor domain
while keeping local PE service behaviour intact.

The first narrow slice of that move is login-session handoff. Relay can now
use Cassandra as the shared store for `loginsession` records so a peer `pe`
replica can repopulate its local RBAC session row on demand instead of
accepting a direct peer database write.

## Shared Orchestration State

The next PE-owned control-plane slice now carried through Fabric is managed
orchestration state.

The current implementation:

- projects the managed `pe-orchestrator` and `pe-inventory` database domain through Fabric and replays it onto peer control-plane replicas
- reserves per-replica sequence residues so replicated inserts do not collide when both replicas create local jobs
- routes compiler PCP brokers, Bolt/orchestrator clients, console traffic, and compiler file-sync through selector-backed `service/pe` so one healthy control-plane replica owns those stable-backend surfaces at a time
- publishes per-pod front-door eligibility and blocker annotations from Relay so `service/pe` promotion is driven by convergence state instead of Pod readiness alone
- keeps Gateway as the transport and health boundary on `8142` and `8143`
- treats `pe-inventory` as persisted connection inventory for saved targets and transport parameters, not as the source of truth for live PCP-connected certnames

That means:

- task and plan execution can survive control-plane failover without shared storage
- orchestration job and plan state can reconverge after a replica returns
- `service/pe` currently uses selector-backed stable routing for those surfaces until they are replica-safe
- an empty `pe-inventory` database during certname-driven PCP execution is currently expected
- broader PCP mediation and any remaining inventory surfaces beyond saved connection records remain open follow-up
- repo helpers now expose that state directly: `scripts/pe-frontdoor-status.sh` shows the current backend and blockers, and `scripts/validate-pe-failover.sh` exercises a live cutover

Like RBAC replay, this database replay path is transitional. The target is to
move durable orchestration job and saved inventory state onto Cassandra-backed
shared Conductor domains rather than copying local PostgreSQL rows between `pe`
replicas.

## Non-Goals

The repo should not treat these as the HA architecture:

- direct PE-to-PE reconciliation of classifier or RBAC state
- direct PE-to-PE Code Manager deploy fanout that bypasses Fabric
- separate Helm releases acting as a fake HA control plane
- compiler-to-compiler coordination for replication
- shared RWX storage between PE instances

Conductor is the control-plane story. Code deployment should stay rooted in supported PE workflows even when Fabric starts carrying deploy intent and convergence state.

## Immediate Implementation Order

The next credible sequence is now:

1. Fabric and Warden foundation
2. Relay insertion on the Puppet Server/PuppetDB path
3. Gateway insertion and sticky orchestration routing on the PCP/orchestrator path
4. Code deployment convergence across Workers
5. Shared classification, RBAC/local-auth, and orchestration job-state convergence
6. Investigation of remaining PCP semantics, saved connection inventory use cases, and reduction of tactical routing exceptions
7. Worker/SPOG role modelling only if a pooled Kubernetes control plane still needs it

That ordering matters because Relay and Gateway depend on Fabric and Warden for identity, trust, and transport, and code convergence needs both paths in place before control-plane traffic can fail over cleanly.
