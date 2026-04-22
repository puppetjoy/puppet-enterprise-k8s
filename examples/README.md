# Example Values

These files are tracked starting points for local operator values.

Copy them into `local/`, then edit them for your registry, cluster, storage
classes, ingress, DNS, and Secrets.

Typical setup:

```bash
mkdir -p local
cp examples/values-pe.example.yaml local/values-pe.yaml
cp examples/values-agent.example.yaml local/values-agent.yaml
cp examples/values-conductor.example.yaml local/values-conductor.yaml
```

`local/values-conductor.yaml` is only required for the supported HA topologies
with multiple `pe` replicas and one or more `pe-compiler` replicas.

The `local/` directory stays operator-specific and is ignored by Git.
