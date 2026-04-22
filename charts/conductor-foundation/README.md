# conductor-foundation Chart

This chart installs the Conductor foundation services used by the PE proof of
concept:

- the Fabric hub
- the Warden controller
- optional Cassandra storage for Conductor-owned shared state

Typical install path:

```bash
helm upgrade --install conductor charts/conductor-foundation \
  --namespace puppet \
  --create-namespace \
  -f local/values-conductor.yaml
```

Use `examples/values-conductor.example.yaml` as the tracked starting point,
then copy it to `local/values-conductor.yaml` and edit it for your
environment.

## Key Values

Use `helm show values charts/conductor-foundation` for the full value set. The
most important values are:

| Value | Purpose |
| --- | --- |
| `hub.image.*` | RabbitMQ image for the Fabric hub |
| `hub.auth.*` | Hub credentials or existing Secret |
| `hub.service.*` | Hub Service ports and type |
| `hub.persistence.*` | Hub storage settings |
| `warden.enabled` | Enable or disable Warden |
| `warden.image.*` | Warden image |
| `warden.intervalSeconds` | Reconciliation interval |
| `warden.pruneStaleParticipants` | Remove stale participants automatically |
| `cassandra.enabled` | Enable the shared-state Cassandra cluster |
| `cassandra.image.*` | Cassandra image |
| `cassandra.replicaCount` | Cassandra StatefulSet replica count |
| `cassandra.persistence.*` | Cassandra storage settings |
| `fabric.segments` | Segment, vhost, and release/workload-set topology |
| `affinity` / `tolerations` / `nodeSelector` | Cluster placement controls |

## Operational Notes

- This chart only provides the Conductor foundation layer.
- PE and compiler pods join that layer from the `puppet-enterprise` chart when
  `conductor.enabled=true`.
- Cassandra is disabled by default. Enable it when you are ready to move
  shared Conductor state off peer PostgreSQL replay and onto a shared backend.
- Segment definitions should match the workload names and namespaces used by
  the PE chart.
