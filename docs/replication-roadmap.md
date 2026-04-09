# Replication Roadmap

This project is moving from a shared-install proof of concept toward a shared-nothing PE architecture.

## Phase 1

Turn the current compiler pool into true worker-local runtime pods:

- local `puppetserver`
- local `puppetdb`
- local `postgresql`
- per-worker non-shared persistent volumes
- centralized Code Manager and file-sync for code delivery
- readiness that reflects code currency and worker-local data health

The immediate goal is to prove that a worker can keep serving from its own local data plane and catch up again after reconnect.

## Phase 2

Split PE-local state from worker-local state:

- keep worker compile-path reads and writes local
- keep PE management data tied to the `pe` workload
- remove remaining worker dependence on PE-local management data for normal catalog service

This is the boundary that makes later multi-PE work tractable.

## Phase 3

Extend replication to the PE control-plane tier with explicit synchronization rather than shared storage.

The current direction for that layer lives in [`../puppet-conductor`](../puppet-conductor), where:

- Relay handles worker-local PuppetDB data propagation
- Gateway fronts orchestration and PCP traffic
- Warden governs mesh membership and trust distribution

That work assumes worker-local PuppetDB and worker-local storage are already in place.

## Phase 4

Move CA handling to an HA-friendly trust model:

- one shared upstream trust anchor
- one intermediate per PE instance or worker
- distributed trust-chain assembly and publication

This is intentionally later work. Worker-local runtime and data boundaries come first.

## Current Acceptance Focus

The next implementation slices should prove:

- a worker compiles using its own local PuppetDB and PostgreSQL
- a worker remains a code-current compile target only when local data services are healthy and PuppetDB sync is fresh
- a worker can fall behind and recover without shared storage
- centralized `puppet-code deploy` still remains the operator entrypoint for code rollout
