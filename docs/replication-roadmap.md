# Replication Roadmap

This project is moving from a shared-install proof of concept toward a shared-nothing PE architecture built from independent PE instances and worker pools.

## Phase 1

This phase is now established in the current chart.

Turn the current compiler pool into true worker-local runtime pods:

- local `puppetserver`
- local `puppetdb`
- local `postgresql`
- per-worker non-shared persistent volumes
- centralized Code Manager and file-sync for code delivery
- readiness that reflects code currency and worker-local data health

This proves the worker data plane can stay local, fall behind, and recover again without shared storage.

## Phase 2

Make one PE instance the control-plane unit that later replication will copy:

- keep PostgreSQL, PuppetDB, the non-compiler Puppet Server, and PE edge/API services tied to one `pe` workload
- keep compiler-local compile and PCP broker traffic on the compiler pool
- keep compiler dependence on PE-local classification, RBAC, CA, and orchestration surfaces explicit
- avoid inventing worker-local substitutes for PE management data

This is the boundary that makes later multi-PE work tractable.

## Phase 3

Extend replication to the PE tier with explicit synchronization rather than shared storage.

The first target in this phase is PE-local management data, not more worker-local caching:

- classification and node-group state
- RBAC and other console-facing metadata
- orchestration-side state and routing
- PE-local awareness of compiler pools and node ownership

The current direction for that layer lives in [`../puppet-conductor`](../puppet-conductor), where:

- Relay handles worker-local PuppetDB data propagation
- Gateway fronts orchestration and PCP traffic
- Warden governs mesh membership and trust distribution

That work assumes worker-local PuppetDB and worker-local storage are already in place, and treats the PE instance as the replicated control-plane unit.

## Phase 4

Move CA handling to an HA-friendly trust model:

- one shared upstream trust anchor
- one intermediate per PE instance or worker
- distributed trust-chain assembly and publication

This is intentionally later work. Independent PE instances and explicit PE-state replication come first.

## Current Acceptance Focus

The next implementation slices should prove:

- two independent PE instances can exist without shared storage
- each PE instance can own its own compiler pool and local state cleanly
- compiler dependencies on PE-local management APIs are explicit and attachable to a chosen PE instance
- centralized `puppet-code deploy` still remains the operator entrypoint for code rollout
