# Replication Roadmap

This repo is moving toward a Conductor-aligned active-active PE architecture. The goal is not shared storage and not ad hoc PE-to-PE reconciliation. The goal is a queue-backed control plane with explicit trust, membership, and health boundaries.

## Baseline

The current chart already proves the foundation we need:

- independent PE instances can run without shared storage
- each PE instance can own its own PostgreSQL, PuppetDB, and PE service state
- each PE instance can own an attached compiler pool with non-shared PVCs
- catalog service and PCP broker traffic can be separated from the PE API/control surface

That baseline is important because Conductor assumes local ownership first and cross-instance coordination second.

## Phase 1: Fabric And Warden

Before any PE state can replicate credibly, the mesh needs a transport and a trust authority.

This phase introduces:

- a local Fabric participant per PE instance
- a separately managed HA Fabric hub
- signed message envelopes and channel isolation
- Warden-driven join approval, onboarding bundles, and key distribution
- Warden assembly of compiled `ca.pem` and merged `crl.pem`

This is the point where active-active HA stops being a Kubernetes deployment pattern and becomes a real distributed system.

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

## Phase 3: Gateway

Gateway brings orchestrator and PCP traffic into the same HA model.

This phase should prove:

- pxp-agent traffic terminates through Gateway
- orchestration inventory can be synchronized through Fabric
- control-plane failure or isolation is reflected in broker readiness and routing
- the compiler and PE service layout still tracks traditional PE responsibilities

## Phase 4: Code Deployment Convergence

The current POC already showed that one PE instance can accept a Code Manager deploy and fan code out to its attached compilers. The active-active extension is to preserve that local PE behaviour while moving inter-Worker coordination onto Fabric.

This phase should prove:

- an operator can trigger Code Manager once on one Worker using the normal PE entrypoint
- the originating Worker can publish signed deploy intent containing the environment and immutable commit SHA
- peer Workers can consume that intent and run their own local Code Manager deploy for the exact revision
- each Worker can publish convergence or failure state for Warden to aggregate
- stale or failed Workers can be marked unhealthy for catalog service until they converge
- attached compilers still receive code from their owning Worker-local PE services rather than through any compiler mesh

## Phase 5: Worker And SPOG Topologies

Once Fabric, Warden, Relay, and Gateway exist, we can model real Conductor roles.

This phase should prove:

- Worker PE instances can serve node traffic behind health-driven load balancing
- SPOG PE instances can consume replicated state without serving catalogs
- trust material and routing decisions stay correct during failure and recovery
- operators can add and remove mesh members through Warden-managed workflows

## Explicit Non-Goals

The following are not the target architecture:

- compiler-to-compiler replication
- direct PE-to-PE classifier reconciliation as the HA mechanism
- direct PE-to-PE Code Manager deploy fanout that bypasses Fabric
- shared RWX storage across PE instances

Code deployment across Workers should remain rooted in supported PE tooling. The Conductor layer can transport deploy intent and convergence state, but it should not replace Code Manager or r10k.
