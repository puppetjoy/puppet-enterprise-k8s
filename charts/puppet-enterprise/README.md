# puppet-enterprise Chart

This chart installs the main Puppet Enterprise runtime for this proof of
concept.

It is intentionally Kubernetes-first:

- PE is installed into per-replica PVCs
- the control plane runs as a `StatefulSet`
- compilers are optional and separate
- multi-replica `pe` uses one stable `service/pe` backend at a time
- Conductor integration is optional and controlled by values

## Before You Install

This chart assumes the operator has already:

- built and pushed a runtime image from a licensed PE installer tarball
- created a repo-local values file
- created any required Secrets, such as the r10k deploy key and optional PE
  license Secret

Use `examples/values-pe.example.yaml` as the tracked starting point, then copy
it to `local/values-pe.yaml` and edit it for your environment.

Typical install path:

```bash
helm upgrade --install pe charts/puppet-enterprise \
  --namespace puppet \
  --create-namespace \
  -f local/values-pe.yaml
```

## Key Values

Use `helm show values charts/puppet-enterprise` for the full value set. The
most important values are:

| Value | Purpose |
| --- | --- |
| `image.repository` / `image.tag` | Runtime image to deploy |
| `network.technicalHostname` | External technical hostname for the control plane |
| `network.compilerHostname` | External compiler hostname |
| `peConfig.consoleAdminPassword` | Initial PE admin password |
| `codeManager.*` | Enable and configure Code Manager |
| `license.secretName` | Existing Secret containing `license.txt` |
| `controlPlane.replicaCount` | Number of `pe` control-plane replicas |
| `controlPlane.ca.provider` | Control-plane CA mode |
| `controlPlane.resources.*` | Per-container control-plane resources |
| `compilers.enabled` | Enable the compiler pool |
| `compilers.replicaCount` | Number of compiler replicas |
| `compilers.resources.*` | Per-container compiler resources |
| `storage.*` | Control-plane PVC sizing and storage classes |
| `conductor.enabled` | Enable participant onboarding and trust integration |
| `conductor.sharedState.cassandra.serviceName` | Default Cassandra Service name used when per-domain contact points are not set |
| `conductor.relay.*` | Enable replicated control-plane state and front-door gating |
| `conductor.relay.classifierSync.*` | Configure the filtered shared classifier graph, including the Cassandra backend |
| `conductor.relay.rbacSync.*` | Configure Cassandra-backed sync for the managed RBAC graph |
| `conductor.relay.rbacTokenSync.*` | Configure Cassandra-backed sync for normal RBAC tokens and shared auth material |
| `conductor.relay.orchestrationSync.*` | Configure persisted orchestration job-state sync, including the Cassandra backend |
| `conductor.relay.inventorySync.*` | Enable Cassandra-backed sync for persisted orchestration inventory |
| `conductor.gateway.*` | Enable Gateway for PCP/orchestration traffic |
| `services.pe.*` | Control-plane Service type and optional load balancer IP |
| `services.compilers.*` | Compiler Service type and optional load balancer IP |
| `ingress.*` | Console ingress exposure |

## Operational Notes

- `service/pe` is the control-plane front door.
- `service/pe-compiler` is the compiler pool front door.
- In multi-replica mode, the chart currently favors stable-backend HA for
  `service/pe` over arbitrary pooled routing.
- When the shared-state sync domains are enabled, the chart defaults them to
  the Conductor Cassandra service unless per-domain contact points are set
  explicitly.
- This chart does not ship tracked environment-specific defaults. Operators are
  expected to supply their own cluster profile in local values files.
- The tracked file `examples/values-pe.example.yaml` is only a starting point,
  not a ready-to-apply cluster profile.

## After Install

Useful repo-level validation commands:

```bash
make pe-frontdoor-status
make validate-pe-failover
```
