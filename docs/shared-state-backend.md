# Shared State Backend

This document defines the intended replacement for the current peer
PostgreSQL replay used by some replicated `pe` control-plane domains.

## Goal

Keep the current Kubernetes deployment shape:

- multiple equivalent `pe` replicas behind `service/pe`
- multiple `pe-compiler` replicas behind `service/pe-compiler`
- no SPOG-only control-plane instance
- no shared RWX storage

while removing the fragile part of the current implementation:

- direct replay of managed PostgreSQL tables between `pe` replicas

## Target Model

The target Conductor state model has three layers:

1. Fabric
   - transport
   - convergence signals
   - trust-aware messaging

2. Cassandra
   - durable shared store for Conductor-owned replicated domains
   - authoritative state shared by all `pe` replicas

3. `pe` replicas
   - execution frontends
   - local caches and local PE service state
   - no peer-to-peer database authority

## What Moves Off Peer PostgreSQL Replay

The current custom PostgreSQL replay should be treated as transitional for:

- RBAC and local-auth managed state
- orchestration job and plan state
- persisted orchestration inventory connections
- login-session handoff

The target is to move those domains onto Cassandra-backed shared state instead
of copying tables from one `pe` replica into another.

## Current Runtime Slices

The first runtime slice is login-session handoff for the console auth barrier.

That path now has a Cassandra-backed implementation available:

- local `pe` login still creates the `loginsession` row in the local RBAC database
- Relay can publish that session record into Cassandra
- another `pe` replica can rehydrate the same session into its local RBAC
  database on demand when a browser request arrives with the session cookie

That keeps PE's local session handling intact while removing direct peer
database writes for that handoff path.

The next runtime slices are persisted orchestration inventory and persisted
orchestration job and plan state:

- Relay can treat `pe-inventory` plus `inventoryKeysJson` as a separate shared
  domain
- the active `pe` replica can publish that snapshot into Cassandra
- peer replicas can rehydrate their local `pe-inventory` database from
  Cassandra-backed shared state instead of replaying peer PostgreSQL rows
- Relay can also publish the managed `pe-orchestrator` snapshot plus
  `orchestratorEncryptionStore` into Cassandra
- peer replicas can rehydrate their local `pe-orchestrator` database from
  Cassandra-backed shared state instead of replaying peer PostgreSQL rows

## What Does Not Need Cassandra

These domains already have a better shape and should stay that way:

- CA and trust material
  - file and bundle convergence, not relational replay
- code deployment
  - deploy intent and convergence state
- classifier semantics
  - logical graph/domain modelling, even if Cassandra eventually stores the
    authoritative graph
- live PCP broker sessions
  - runtime locality, not durable shared authority

## Local PostgreSQL Role After Migration

Local PE PostgreSQL remains useful, but as:

- local PE service backing store
- cache or projection of shared state where PE still expects relational data
- restart-time reconstruction target

It should no longer be the cross-replica source of truth for replicated
domains.

## First Migration Slices

The recommended order is:

1. login-session state
   - smallest current direct DB sync path
   - good candidate for a Cassandra-backed Conductor domain

2. persisted orchestration inventory
   - saved connections are durable shared state
   - now available on the Cassandra-backed path

3. persisted orchestration jobs and plans
   - naturally event- and record-oriented
   - now available on the Cassandra-backed path

4. classifier shared graph
   - move from projected peer replay toward Conductor-owned authoritative graph

5. RBAC and local auth
   - last, because it is the most tightly coupled to current PE local schema

## Foundation Requirement

The `conductor-foundation` chart now grows the shared-state layer by adding
optional Cassandra infrastructure. That is the durable backend for the new
direction. The architectural commitment here is shared state in Cassandra, not
a new durable middle tier.
