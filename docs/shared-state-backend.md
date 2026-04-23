# Shared State Backend

This document describes the current shared-state model for replicated `pe`
control-plane domains.

The earlier peer PostgreSQL replay path is no longer the preferred
architecture. The current direction is Cassandra-backed shared state with local
PE PostgreSQL left in place as an execution-local projection.

## Goal

Keep the current Kubernetes deployment shape:

- multiple equivalent `pe` replicas behind `service/pe`
- multiple `pe-compiler` replicas behind `service/pe-compiler`
- no SPOG-only control-plane instance
- no shared RWX storage

while removing the most fragile part of the earlier implementation:

- direct replay of managed PostgreSQL tables between `pe` replicas

## Current Model

The shared-state model now has three layers:

1. Fabric
   - transport
   - convergence signals
   - trust-aware messaging

2. Cassandra
   - durable shared store for Conductor-owned replicated domains
   - authoritative state shared by the `pe` replicas

3. `pe` replicas
   - execution frontends
   - local relational projection where PE still expects PostgreSQL
   - no peer-to-peer database authority

Fabric is not the shared-state database. Fabric carries transport and
convergence signals. Cassandra is the durable shared authority for the
migrated domains.

## How Rehydration Works

The current repo uses two rehydration patterns.

### Request-Time Lazy Rehydration

This is used for auth paths where the first request after failover must work.

- Relay's auth barrier intercepts the incoming session-cookie or bearer-token
  request before it reaches PE
- if the needed local RBAC projection row is missing, Relay reads the
  authoritative record from Cassandra
- Relay recreates the local row in the replica's own PE PostgreSQL
- Relay then proxies the original request upstream to the local PE service

That is the current pattern for login-session handoff and normal RBAC tokens.

### Background Projection

This is used for the larger replicated control-plane domains.

- Relay publishes convergence signals and hashes over Fabric
- peers treat Fabric as intent, not as the shared-state source
- each peer reads the authoritative snapshot from Cassandra
- each peer refreshes its own local PE PostgreSQL or classifier projection

That is the current pattern for the managed classifier graph, managed RBAC
graph, persisted inventory connections, and persisted orchestrator job and
plan state.

### Why Eligibility Still Matters

Background projection is intentionally done before a replica is trusted with
the `service/pe` front door.

- Relay publishes blocker and eligibility annotations for its pod
- `conductor-service-selector` reads those annotations from the Kubernetes API
- the selector labels one eligible pod as the active `service/pe` backend

This keeps most shared-state catch-up out of the request path and leaves
request-time lazy rehydration for the narrower auth surfaces that need it.

## Current Cassandra-Backed Domains

These domains now use Cassandra-backed shared state:

- filtered managed classifier graph
- login-session handoff
- managed RBAC graph
- normal RBAC tokens and shared auth files
- persisted orchestration inventory connections
- persisted orchestration job and plan state

For these domains, the active `pe` replica writes shared state into Cassandra
and peer replicas rehydrate their local PE PostgreSQL state from Cassandra when
needed.

## Domains That Stay Local Or Use A Different Model

These parts of the system are intentionally not Cassandra-backed:

- CA and trust material
  - replicated files and trust bundles, not relational state
- code deployment
  - deploy intent and convergence state
- live PCP broker sessions and discovered PCP connections
  - runtime-local state on the selected backend
- service-local diagnostics and activity fields
  - useful locally, but not authoritative shared state

## Local PostgreSQL After Migration

Local PE PostgreSQL still matters. Its role is now:

- local PE service backing store
- local projection of shared control-plane state where PE expects relational
  data
- restart-time reconstruction target

It is no longer the intended cross-replica source of truth for the migrated
domains.

## Why This Is Simpler

The current model avoids:

- direct table replay from one `pe` replica into another
- sequence-collision management as the main replication mechanism
- peer PostgreSQL split-brain as the authority model

It keeps the current deployment shape while moving durable shared state into a
backend that is designed to be shared.

## Current Follow-On Work

The remaining work in this area is mostly operational:

- Cassandra auth and TLS
- backup and restore proof
- chart hardening and clearer operational defaults
- removal of any dead code or docs that still assume peer PostgreSQL replay

The main architectural move is already in place: Cassandra is now the shared
backend story for the replicated control-plane domains in this repo.
