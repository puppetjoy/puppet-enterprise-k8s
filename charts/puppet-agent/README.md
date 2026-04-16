# puppet-agent Chart

This chart deploys a test-oriented Puppet agent workload for exercising a PE
release from inside Kubernetes.

It exists to validate:

- certificate issuance and signing
- catalog compilation through the compiler front door
- report submission
- PXP task and plan execution

Typical install path:

```bash
helm upgrade --install test-node charts/puppet-agent \
  --namespace puppet \
  --create-namespace \
  -f local/values-agent.yaml
```

Use `examples/values-agent.example.yaml` as the tracked starting point, then
copy it to `local/values-agent.yaml` and edit it for your environment.

## Key Values

Use `helm show values charts/puppet-agent` for the full value set. The most
important values are:

| Value | Purpose |
| --- | --- |
| `image.repository` / `image.tag` | Agent image |
| `agent.server` | Main server or compiler front door for catalog traffic |
| `agent.caServer` | Optional explicit CA server |
| `agent.serverList` | Optional PE `server_list` override |
| `agent.packageRepoServer` / `agent.packageRepoUrl` | Package repository location |
| `agent.environment` | Agent environment |
| `agent.certname` | Optional fixed certname |
| `agent.certnameSuffix` | Suffix when per-pod certnames are generated |
| `agent.pxpEnabled` | Enable or disable `pxp-agent` |
| `signer.enabled` | Create a signer Job for the test certificate |
| `signer.peReleaseName` | PE release to target for signing |
| `signer.image.*` | Runtime image used by the signer Job |
| `storage.*` | PVC sizing and storage classes |

## Operational Notes

- This is not a general-purpose agent chart.
- Its main purpose is validation against the current PE implementation.
- By default it fits the repo’s failover harness and validation scripts.
