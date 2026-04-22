# conductor-foundation Chart

This chart installs the Conductor foundation services used by the PE proof of
concept:

- the Fabric hub
- the Warden controller
- Cassandra storage for Conductor-owned shared state when the selected PE
  topology requires it

Typical install path:

```bash
make deploy-conductor
```

If you invoke Helm directly instead of using the repo Makefile, pass
`topology.controlPlaneReplicaCount` and `topology.compilerReplicaCount` so the
chart can decide whether the selected topology needs the foundation release.

Use `examples/values-conductor.example.yaml` as the tracked starting point,
then copy it to `local/values-conductor.yaml` and edit it for your
environment. That file is only required for the supported HA topologies with
multiple `pe` replicas and one or more compilers.

## Key Values

Use `helm show values charts/conductor-foundation` for the full value set. The
most important values are:

| Value | Purpose |
| --- | --- |
| `hub.image.*` | RabbitMQ image for the Fabric hub |
| `hub.auth.*` | Hub credentials or existing Secret |
| `hub.service.*` | Hub Service ports and type |
| `hub.persistence.*` | Hub storage settings |
| `foundation.mode` | `auto`, `enabled`, or `disabled` control for the release |
| `topology.controlPlaneReplicaCount` | Control-plane replica count used by `auto` mode |
| `topology.compilerReplicaCount` | Compiler replica count used by `auto` mode |
| `warden.enabled` | Enable or disable Warden |
| `warden.image.*` | Warden image |
| `warden.intervalSeconds` | Reconciliation interval |
| `warden.pruneStaleParticipants` | Remove stale participants automatically |
| `cassandra.image.*` | Cassandra image |
| `cassandra.replicaCount` | Cassandra StatefulSet replica count |
| `cassandra.persistence.*` | Cassandra storage settings |
| `fabric.segments` | Segment, vhost, and release/workload-set topology |
| `affinity` / `tolerations` / `nodeSelector` | Cluster placement controls |

## Operational Notes

- This chart only provides the Conductor foundation layer.
- In `foundation.mode=auto`, this chart renders only for the supported HA
  topologies with multiple `pe` replicas and one or more compilers.
- `make deploy-conductor` derives the topology counts from
  `local/values-pe.yaml`.
- PE and compiler pods join this layer from the `puppet-enterprise` chart when
  that topology requires Conductor participation.
- Cassandra is the shared-state backend used by the current replicated
  control-plane domains in this repo.
- Segment definitions should match the workload names and namespaces used by
  the PE chart.
