# Replication Roadmap

This document tracks the work that still matters after the current proof
points. It is not a history of every phase already completed.

## Current Baseline

The repo now proves:

- rebuild from repo workflows
- stable-backend HA through `service/pe`
- automatic failover and standby re-entry
- separate compiler scale tier through `service/pe-compiler`
- Conductor transport, onboarding, and trust distribution
- Cassandra-backed shared state for the current replicated control-plane
  domains
- live HA validation for code deploy, orchestration, agent traffic, and CA
  lifecycle

## Remaining Work

### Cassandra Operations

The main remaining architectural work is now operational, not conceptual:

- backup and restore proof
- auth and TLS
- clearer operational defaults and maintenance guidance

### Remaining Locality Boundaries

Some PE behavior still depends on a selected control-plane backend. The open
question is not whether everything must be pooled. The open question is which
surfaces are worth pushing further versus leaving behind the current
stable-backend boundary.

Likely candidates for more work are:

- remaining console-backed behavior
- any additional orchestration or PCP state that should be shared rather than
  local
- any remaining service assumptions that complicate failover or rebuild

### Validation And Recovery

The current validation harness is strong, but the follow-on work is to make the
operational story easier to repeat:

- backup and restore validation
- clearer day-2 recovery workflows
- less dependence on repo-specific operator knowledge during validation

## Deferred Beyond The PoC

These are reasonable next-stage items, but they are not required to explain or
prove the current PoC:

- chart hardening
- stricter Cassandra operational defaults
- more polished operator UX
- broader documentation cleanup around day-2 operations

## Non-Goals

The repo is not moving toward:

- shared RWX storage between PE instances
- compiler-to-compiler replication
- separate Helm releases acting as one HA mesh
- a required SPOG-only topology
- direct peer PostgreSQL replay as the long-term control-plane model
