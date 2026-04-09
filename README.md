# Puppet Enterprise on Kubernetes

This repo is the Kubernetes-oriented follow-on to the earlier container proof of concept.

The design goal is not "run the old Docker container in Kubernetes." It is:

- build one AlmaLinux 9 PE runtime image with the official PE tarball staged inside it
- run an installer Job once with a generated `pe.conf` ConfigMap and optional license Secret
- persist the install output onto Kubernetes volumes
- run PE components as separate Kubernetes workloads without systemd

The current scaffold includes:

- a PE runtime image in `image/`
- a development test-agent image in `agent-image/`
- Helm charts in `charts/`
- docs mapping the legacy systemd services to Kubernetes workloads in `docs/`

## Current Model

The image contains:

- AlmaLinux 9 userland
- the PE installer tarball extracted under `/opt/pe-installer`
- deterministic `pe-*` users and groups
- entrypoint, installer, role-runner, and probe scripts

The installer Job mounts:

- `/etc/puppetlabs`
- `/opt/puppetlabs`
- `/var/lib/pe-k8s`

and runs the official `puppet-enterprise-installer` with a mounted `pe.conf`.

After install, the Job exports runtime artifacts that are created outside the mounted PE trees, primarily:

- `/etc/sysconfig/pe-*`
- `/etc/sysconfig/pe-pgsql`
- an install marker and summary under `/var/lib/pe-k8s/install/`

Runtime workloads mount the same PVCs, restore the exported sysconfig files into their local rootfs, and run their PE service in foreground mode.

## Workload Shape

The initial chart is split into:

- `installer` Job
- `postgresql` StatefulSet
- `puppetdb` Deployment
- `puppetserver` Deployment
- PE services `Deployment`

The PE services deployment uses multiple containers from the same image:

- `nginx`
- `console-services`
- `orchestration-services`
- `bolt-server`
- `ace-server`
- `host-action-collector`

This keeps one PE service per container while still colocating the tightly-coupled HTTP/API edge services.

## Build

Stage a PE tarball into the Kubernetes runtime image with:

```bash
CONTAINER_ENGINE=podman \
make build-k8s-runtime \
  PE_VERSION=2025.9.0 \
  PE_INSTALLER_TAR_PATH=/absolute/path/to/puppet-enterprise-2025.9.0-el-9-x86_64.tar.gz
```

That stages the installer into `image/assets/pe-installer/installer.tar.gz` for the build and removes it afterward.

Build the development test-agent image with:

```bash
CONTAINER_ENGINE=podman \
make build-k8s-agent \
  K8S_AGENT_IMAGE_NAME=registry.example.test/pe-k8s-agent \
  K8S_AGENT_IMAGE_VERSION=0.1.1
```

This image is intentionally for validation and stack exercise, not production use.

## Helm

Render the chart:

```bash
helm template pe charts/puppet-enterprise
```

Install with generated PE config values:

```bash
helm upgrade --install pe charts/puppet-enterprise \
  --namespace puppet \
  --create-namespace \
  --set image.repository=registry.eyrie/pe-k8s-runtime \
  --set image.tag=2025.9.0 \
  --set peConfig.consoleAdminPassword='dummyPassword1!' \
  --set network.technicalHostname=pe.example.test \
  --set ingress.enabled=true \
  --set ingress.host=puppet.example.test
```

The chart generates these `pe.conf` values automatically as HOCON from the Helm release and network settings:

- `puppet_enterprise::certname`
- `puppet_enterprise::puppet_master_host`
- `puppet_enterprise::profile::master::dns_alt_names`

Generated SANs always include the in-cluster service names for the release, and can add:

- one external technical hostname via `network.technicalHostname`
- extra SANs via `network.additionalDnsAltNames`

By default, the chart keeps `puppet_enterprise::puppet_master_host` on the in-cluster `service/pe` name even when you set `network.technicalHostname`. If you need a different runtime host value, override `peConfig.puppetMasterHost`. You can also override `peConfig.certname` explicitly, but the default is the Helm-generated `pe` or `pe-<release>` identity.

For settings that are not modeled yet, append raw installer config with `peConfig.extra`.

Code Manager can also be modeled directly from Helm values:

- set `codeManager.enabled=true`
- set `codeManager.r10kRemote`
- provide `codeManager.r10kKnownHosts`
- provide the deploy key with an existing Kubernetes Secret via `codeManager.r10kPrivateKeySecretName`

Example:

```bash
kubectl -n puppet create secret generic pe-r10k-deploy-key \
  --from-file=r10k-deploy-key=/path/to/r10k-private-key

helm upgrade --install pe charts/puppet-enterprise \
  --namespace puppet \
  --create-namespace \
  -f local/values-eyrie.yaml \
  --set codeManager.enabled=true \
  --set codeManager.r10kRemote=git@gitlab.example.test:org/control-repo.git \
  --set codeManager.r10kPrivateKeySecretName=pe-r10k-deploy-key
```

The chart renders these Code Manager `pe.conf` settings when enabled:

- `puppet_enterprise::profile::master::code_manager_auto_configure`
- `puppet_enterprise::profile::master::r10k_remote`
- `puppet_enterprise::profile::master::r10k_private_key`
- `puppet_enterprise::profile::master::r10k_known_hosts`
- `puppet_enterprise::profile::master::r10k_remote_timeout`

The deploy key is mounted from the Secret into the installer Job, `deployment/pe`, and `deployment/pe-puppetserver` at `codeManager.r10kPrivateKeyPath`. For an existing release, set `installer.forceReinstall=true` for the upgrade that introduces or materially changes Code Manager configuration so PE re-runs `puppet infrastructure configure`.

If the Git remote hostname needs a Kubernetes-specific override, set `network.hostAliases`. This is useful when the SSH endpoint for the control repo resolves differently inside the cluster than it does on an operator workstation.

Naming follows the Helm release name:

- release `pe` renders base resources like `pe`, `pe-puppetserver`, and `pe-puppetdb`
- release `foo` renders `pe-foo`, `pe-foo-puppetserver`, and `pe-foo-puppetdb`

The chart currently defaults to the `puppet` namespace and assumes the `owl-crypt` storage class for the initial scaffold because `/etc/puppetlabs`, `/opt/puppetlabs`, and `/var/lib/pe-k8s` are currently shared across workloads.

That is a starting point, not the final HA design.

The default `eyrie` scheduling and exposure assumptions are:

- pod affinity/tolerations pin all workloads to nodes labeled `node-role.kubernetes.io/compute`
- PVCs default to `owl-crypt`
- ingress support is wired for `contour-compute`
- cert-manager integration defaults to the `eyrie-ca` `ClusterIssuer`

Ingress is disabled by default until you choose a host and are ready to expose the web console.

The intended access pattern is:

- `service/pe` is the technical front door for PE APIs and agent-facing mTLS traffic
- ingress points at `service/pe` for the console hostname, with TLS terminated by the ingress controller
- `service/pe-puppetserver`, `service/pe-puppetdb`, and `service/pe-postgresql` remain the backend service boundaries

## Validation Agents

For development testing, the PE chart can enable naive autosigning and a separate `puppet-agent` chart can create persistent Kubernetes-backed test nodes.

Currently supported autosign modes on the PE chart are:

- `off`
- `naive`

Enable naive autosigning in the PE release with:

```bash
helm upgrade --install pe charts/puppet-enterprise \
  --namespace puppet \
  -f local/values-eyrie.yaml \
  --set testAgents.autosign.mode=naive
```

The separate validation-node chart lives at `charts/puppet-agent/`. It creates a StatefulSet-backed agent node that:

- installs `puppet-agent` from the PE package repo at runtime
- bootstraps SSL automatically against the PE CA
- runs one immediate `puppet agent -t`
- stays up as a long-running agent so you can re-test code deployment and reporting later

Install a test node with:

```bash
helm upgrade --install test-node charts/puppet-agent \
  --namespace puppet \
  -f local/values-agent-eyrie.yaml
```

Default behavior:

- release `test-node` creates pod `test-node-puppet-agent-0`
- the default certname becomes `test-node-puppet-agent-0.test.puppet`
- the default PE package repo is `https://pe:8140/packages/current/el-9-x86_64.repo`
- the default PE server and CA are both `pe`

This helper path is useful for validating:

- certificate issuance and autosigning behavior
- catalog compilation through `service/pe`
- facts, catalogs, and reports landing in PuppetDB
- Code Manager and control-repo changes from a real agent run

## Important Boundary

This scaffold is intentionally Kubernetes-first, but not yet production-ready.

Open design work remains around:

- storage partitioning of `/opt/puppetlabs/server/data/*`
- safe multi-replica scaling of PE services
- upgrade orchestration
- ownership and security hardening
- secrets and certificate rotation

The mapping doc in `docs/legacy-service-mapping.md` is the source of truth for the next decomposition steps.

## Certificate Regeneration

The chart now has a disabled-by-default maintenance Job for SAN changes:

```bash
helm upgrade pe charts/puppet-enterprise \
  --namespace puppet \
  -f local/values-eyrie.yaml \
  --set certificateRegeneration.enabled=true
```

The intended workflow is:

1. Update `network.technicalHostname`, `network.additionalDnsAltNames`, or any explicit `peConfig` overrides.
2. Apply those values so the desired `pe.conf` is stored in the release.
3. Leave `deployment/pe`, `deployment/pe-puppetserver`, and `deployment/pe-puppetdb` up so the maintenance job can reach the CA and PuppetDB through the normal in-cluster `pe` service.
4. Run one Helm upgrade with `certificateRegeneration.enabled=true`.
5. Wait for the `*-cert-regen-r<revision>` Job to complete.
6. Roll the workloads so the running processes pick up the refreshed cert material.

Because PE's built-in node-role verification does not map cleanly onto the split Kubernetes layout, the chart defaults `certificateRegeneration.force=true`. The maintenance Job still compares the current certificate SANs to the desired SAN set and exits without changes when they already match, unless you explicitly override `certificateRegeneration.force`.

On success, the regeneration Job also refreshes the copied `pe.cert.pem` / `pe.private_key.pem` / `pe.private_key.pk8` material used by the split services.

## Certificate Recovery

If the regeneration Job fails after revoking the old host cert, use the recovery Job to rebuild the host cert from the on-disk CA and repopulate the split service SSL directories:

```bash
helm upgrade pe charts/puppet-enterprise \
  --namespace puppet \
  -f local/values-eyrie.yaml \
  --set certificateRecovery.enabled=true
```

Recovery workflow:

1. Scale `deployment/pe`, `deployment/pe-puppetserver`, and `deployment/pe-puppetdb` down to `0`.
2. Run one Helm upgrade with `certificateRecovery.enabled=true`.
3. Wait for the `*-cert-recover-r<revision>` Job to complete.
4. Scale the workloads back up with a normal Helm upgrade.

The recovery Job regenerates the host cert offline from the CA files on the PVCs and rewrites the copied `pe.cert.pem`, `pe.private_key.pem`, and `pe.private_key.pk8` files for:

- `puppetdb`
- `orchestration-services`
- `console-services`
- `host-action-collector`
- `bolt-server`
- `ace-server`
- `patching-service`
- `infra-assistant`
- `workflow-service`
