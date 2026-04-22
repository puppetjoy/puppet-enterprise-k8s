# Replication Roadmap

This is the engineering roadmap for what still needs to happen after the
current proof points. It is intentionally more forward-looking and contributor-
oriented than the top-level [README](../README.md) or
[solution overview](solution-overview.md).

This repo is moving toward a Conductor-aligned active-active PE architecture.
The goal is not shared storage and not ad hoc PE-to-PE reconciliation. The
goal is a queue-backed control plane with explicit trust, membership, and
health boundaries.

## Baseline

The current chart already proves the foundation we need:

- independent PE instances can run without shared storage
- each PE instance can own its own PostgreSQL, PuppetDB, and PE service state
- each PE instance can own an attached compiler pool with non-shared PVCs
- catalog service and PCP broker traffic can be separated from the PE API/control surface

Those separate releases are useful as isolated sandboxes, but they are not the HA domain. The HA target is release-internal replication: multiple control-plane replicas and multiple compiler replicas inside one release. The baseline is still important because Conductor assumes local ownership first and cross-participant coordination second.

Kubernetes interpretation matters here. For this repo, the target is not to
recreate a VM-era split literally inside the cluster. The target is to make the
replicas inside one release equivalent enough that `service/pe` can stay the
pooled control-plane front door. Worker and SPOG should be treated as logical
traffic roles. A SPOG-only topology is optional future work, not a prerequisite
for proving active-active HA in Kubernetes.

## Phase 1: Fabric And Warden

Before any PE state can replicate credibly, the mesh needs a transport and a trust authority.

This phase introduces:

- a local Fabric participant per stable replicated workload member inside a release
- a separately managed HA Fabric hub
- signed message envelopes and channel isolation
- Warden-driven join approval, onboarding bundles, key distribution, and stale-participant pruning
- Warden assembly of compiled `ca.pem` and merged `crl.pem`

This is the point where active-active HA stops being a Kubernetes deployment pattern and becomes a real distributed system.

Current status:

- Warden can expand the `pe` and compiler `StatefulSet` members inside one release into onboarding bundles
- optional `conductor-participant` sidecars can join the Fabric hub as those per-pod identities
- control-plane pods can publish local trust sources and Warden can assemble a segment `ca.pem` and `crl.pem` bundle
- participant readiness can now gate `service/pe` and `service/pe-compiler` on onboarding, Fabric connectivity, and trust-bundle installation
- replicated control-plane state is still outstanding

## Phase 2: Relay

Relay is the first data-plane Conductor component we need inside the PE runtime path.

This phase should prove:

- PuppetDB writes can be published into Fabric while preserving local PuppetDB behaviour
- PuppetDB reads remain local-only
- facts replicate to other Workers and SPOGs
- reports replicate to SPOGs
- catalog resource replication is optional and explicit
- readiness for catalog service can be tied to Relay and trust health

This is where we start earning real Worker semantics instead of just running multiple isolated PE stacks.

Current status:

- an optional `conductor-relay` sidecar now runs inside the PE and compiler workload shapes
- Relay reuses the per-pod onboarding bundle, publishes local PuppetDB health into Fabric, and stores fresh peer Relay status snapshots
- Puppet Server runtime config now keeps local `server_urls` and points `submit_only_server_urls` at the pod-local Relay command proxy
- Relay now captures selected PuppetDB submit-only commands and replays facts, reports, and deactivate-node commands to the control-plane role
- Relay readiness can now gate a pod on participant trust plus role-aware local PuppetDB health
- full catalog replication is still intentionally out of scope for the current slice

## Phase 3: Gateway

Gateway brings orchestrator and PCP traffic into the same HA model.

This phase should prove:

- pxp-agent traffic terminates through Gateway
- persisted orchestration connection inventory can be synchronized through Fabric
- control-plane failure or isolation is reflected in broker readiness and routing
- the compiler and PE service layout still tracks traditional PE responsibilities

Current status:

- an optional `conductor-gateway` sidecar now runs inside the PE control-plane workload shape
- `service/pe` and `pe-headless` can target Gateway listener ports for `8142` PCP broker traffic and `8143` orchestration traffic instead of targeting the orchestration container directly
- multi-replica control planes now use selector-backed `service/pe` routing so console, compiler file-sync, compiler brokers, and Bolt/orchestrator clients share one healthy control-plane backend for stable traffic
- Gateway proxies those TCP flows to the pod-local orchestration service, publishes Gateway status into Fabric, and stores fresh peer Gateway snapshots locally
- Gateway readiness is tied to participant trust readiness plus local PCP broker and orchestration health, so stale or disconnected control-plane replicas fall out of service routing
- live validation now covers Bolt task execution, plan execution, and selector failover from `pe-1` to `pe-0`, with compiler brokers reconnecting to the surviving control-plane replica
- selector promotion is now driven by Relay-published front-door eligibility, with blocker annotations on each `pe` pod and repo helpers for front-door status and failover validation
- current evidence indicates that `pe-inventory` stores saved connection inventory rather than live PCP broker presence, so broader PCP message mediation is still outstanding

## Phase 4: Code Deployment Convergence

The current POC already showed that one PE instance can accept a Code Manager deploy and fan code out to its attached compilers. The active-active extension is to preserve that local PE behaviour while moving inter-Worker coordination onto Fabric.

This phase should prove:

- an operator can trigger Code Manager once on one Worker using the normal PE entrypoint
- the originating Worker can publish signed deploy intent containing the environment, deploy signature, deploy identifier, and origin file-sync metadata
- peer Workers can consume that intent and run their own local Code Manager deploy
- each Worker can publish convergence or failure state for Warden to aggregate
- stale or failed Workers can be marked unhealthy for catalog service until they converge
- attached compilers still receive code from release-local PE services rather than through any compiler mesh

Current POC status:

- implemented with a relay-owned Code Manager post-environment hook and Fabric message type
- verified live in Kubernetes with a deploy triggered on `pe-0` and replayed on `pe-1`
- relay readiness now drains a Worker until its local deploy signature matches the desired deploy signature
- origin file-sync commit metadata is preserved for visibility, but cross-Worker equality is based on deploy signature because PE file-sync commit IDs are instance-local
- compiler runtime now patches PE file-sync client URLs to `service/pe`, which avoids cross-replica file-sync/object mismatches by keeping control-plane traffic on one selected backend during convergence

## Phase 5: Worker And SPOG Topologies

Once Fabric, Warden, Relay, and Gateway exist, we can model real Conductor roles.

This phase should prove:

- Worker-eligible `pe` replicas can serve node traffic behind health-driven load balancing through `service/pe`
- a future SPOG topology, if we decide we need one, can consume replicated state without serving catalogs
- trust material and routing decisions stay correct during failure and recovery
- operators can add and remove mesh members through Warden-managed workflows

Current decision:

- do not treat permanent worker pinning as the target architecture
- do not introduce SPOG-only replicas unless they solve a real Kubernetes problem that pooled `service/pe` cannot solve cleanly
- prefer making pooled control-plane replicas equivalent over introducing topology splits inherited from bare metal or VM deployments

## Phase 6: Shared State Backend

The current custom PostgreSQL replay used for some shared control-plane
domains is useful as a proof point, but it is not the intended end state.

This phase should prove:

- Cassandra can act as the durable shared backend for Conductor-owned control-plane domains
- `pe` replicas can stay equivalent behind `service/pe` without peer-to-peer database replay
- the shift does not introduce a new durable middle-tier service
- local PE databases can become execution-local caches or projections instead of cross-replica authority

Current direction:

- `conductor-foundation` now grows optional Cassandra infrastructure as the shared-state layer
- peer PostgreSQL replay for RBAC, orchestration, and login-session handoff is now treated as transitional
- the first migration slices should be login sessions, orchestration state, and only then broader auth domains such as RBAC
- classifier should evolve toward a Conductor-owned authoritative graph rather than more local-database replay

## Phase 7: PE-Owned State Convergence

Once transport, trust, PuppetDB command relay, Gateway, and code deployment
convergence exist, the next job is to remove the remaining replica-local PE
surfaces that make pooled `service/pe` unsafe for some traffic classes.

This phase should prove:

- user-managed classifier state can converge across `pe` replicas without cloning PE's built-in infrastructure groups
- readiness can reflect whether a control-plane replica is current enough for shared PE-owned state
- broader console-backed state can move through Fabric rather than through ad hoc direct PE-to-PE repair
- tactical routing exceptions can shrink as replica equivalence improves

Current status:

- relay now projects a filtered managed classifier domain rooted at `All Nodes` instead of exposing a Conductor-specific user subtree
- `All Environments` and `PE Patch Management` are synchronized by semantic anchor, so their local installer-created IDs can differ while their contents still converge
- PE-owned local infrastructure roots such as `PE Infrastructure` remain outside the managed sync domain
- live validation in Kubernetes confirmed create and delete convergence for managed groups between `pe-0` and `pe-1`
- RBAC and local-auth managed state now converge across `pe` replicas, including cross-replica token validation for normal user tokens
- managed orchestration job state now converges across `pe` replicas, with matching `pe-orchestrator` row counts after failover and recovery
- RBAC and orchestration still rely on transitional peer PostgreSQL replay and are candidates for Cassandra-backed replacement
- console session behaviour and other remaining console-backed writes are still outstanding

## Explicit Non-Goals

The following are not the target architecture:

- compiler-to-compiler replication
- direct PE-to-PE classifier reconciliation as the HA mechanism
- direct PE-to-PE Code Manager deploy fanout that bypasses Fabric
- treating separate Helm releases as one active-active mesh
- shared RWX storage across PE instances

Code deployment across Workers should remain rooted in supported PE tooling. The Conductor layer can transport deploy intent and convergence state, but it should not replace Code Manager or r10k.
