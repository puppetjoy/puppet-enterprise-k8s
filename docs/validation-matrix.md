# Validation Matrix

This document maps the main behaviors in the current implementation to the
tooling that demonstrates them.

## Operator Entry Points

The repo currently exposes these top-level validation commands:

| Command | Purpose |
| --- | --- |
| `make check-current-state PE_VERSION=<version>` | Confirm repo-local inputs are present before build/deploy |
| `make pe-frontdoor-status` | Show the selected `service/pe` backend and standby eligibility |
| `make validate-pe-failover` | Run the full live HA harness |
| `make validate-stack` | Run the front-door status check and then the HA harness |

## Checks

| Claim | Tooling | What success looks like |
| --- | --- | --- |
| Local build/deploy inputs are present | `make check-current-state PE_VERSION=<version>` | Local values files, SSH key, and installer tarball are found |
| The charts deploy from repo-managed workflows | `make deploy-conductor && make deploy-pe && make deploy-agent` | Helm upgrades/installations succeed |
| `service/pe` has a clear active backend | `make pe-frontdoor-status` | One active `pe` backend is shown and another replica is eligible or standby |
| The control plane fails over cleanly | `make validate-pe-failover` | The harness deletes the active `pe` backend and observes a new active backend |
| Code deploy converges through the control plane | `make validate-pe-failover` | The harness completes a `puppet code deploy` step before and after failover |
| Catalog traffic survives failover | `make validate-pe-failover` | Test agent runs succeed through `pe-compiler.eyrie` before and after failover |
| Managed classifier groups converge across replicas | `make validate-pe-failover` | The harness creates a temporary node group on one `pe` replica, sees it on the peer, then verifies the delete converges too |
| Orchestration survives failover | `make validate-pe-failover` | Task and plan runs succeed before and after failover |
| Managed RBAC state converges across replicas | `make validate-pe-failover` | The harness creates a temporary user on one `pe` replica, sees it on the peer, and authenticates that user there |
| Persisted orchestration job history survives replica changes | `make validate-pe-failover` | The harness creates a Puppet job on one `pe` replica and reads it back from another |
| Fresh enrollment and signing work | `make validate-pe-failover` | A new test agent cert is requested, signed, and used successfully |
| CA revocation propagates to compilers | `make validate-pe-failover` | The revoked test cert is rejected through `pe-compiler.eyrie` |
| Shared-state failover survives partial Cassandra loss | `make validate-pe-failover` | The harness degrades the Cassandra cluster, exercises control-plane failover, and then verifies recovery |
| Standby re-entry is automatic | `make validate-pe-failover` | The failed control-plane pod returns as an eligible standby without manual repair |
| The full validation run is reproducible from one command | `make validate-stack` | Front-door status prints first, then the HA harness completes |

## Scope Of `validate-pe-failover`

The current HA harness exercises:

1. Console reachability
2. Code deployment
3. Agent run through the compiler front door
4. Managed classifier projection across replicas
5. Task execution
6. Plan execution
7. Managed RBAC projection across replicas
8. Cross-replica Puppet job history visibility
9. Fresh agent enrollment and signing
10. Control-plane failover
11. Post-failover revalidation
12. Cassandra degradation, failover under degradation, and member recovery
13. Revoke and clean
14. CRL pickup and revoked-cert rejection
15. Standby re-entry, then post-failover classifier, RBAC, and job-history visibility

## Notes

- `make validate-stack` is a convenience wrapper, not a second validation
  engine.
- The repo does not currently expose a destructive one-shot namespace rebuild
  target. Rebuild validation is still performed as an operator workflow using the
  existing build and deploy targets.
- `make pe-frontdoor-status` is useful both before and after `make
  validate-pe-failover` to confirm selector behavior and standby state.
