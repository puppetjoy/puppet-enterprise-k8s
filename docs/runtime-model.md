# Runtime Model

This document captures the lower-level runtime and operational detail that is useful for implementers, but too specific for the top-level README.

## Current Model

The image contains:

- AlmaLinux 9 userland
- the PE installer tarball extracted under `/opt/pe-installer`
- deterministic `pe-*` users and groups
- entrypoint, installer, role-runner, and probe scripts

The `pe` workload runs an install init container that mounts:

- `/etc/puppetlabs`
- `/opt/puppetlabs`
- `/var/lib/pe-k8s`

and runs the official `puppet-enterprise-installer` with a mounted `pe.conf`.

After install, the init container exports runtime artifacts that are created outside the mounted PE trees, primarily:

- `/etc/sysconfig/pe-*`
- `/etc/sysconfig/pe-pgsql`
- an install marker and summary under `/var/lib/pe-k8s/install/`

The `pe` runtime containers mount those same per-replica persistent volumes, restore the exported sysconfig files into their local rootfs, and run their PE services in foreground mode.

Compiler replicas follow the same pattern on per-replica persistent volumes:

- `install-compiler-runtime` init installs the PE compiler-local packages
- `bootstrap-compiler` init enrolls the compiler, configures local PuppetDB/PostgreSQL, and applies the compiler catalog
- runtime containers then run local `postgresql`, `puppetdb`, and `puppetserver`
- compiler `puppetserver` also exposes the PCP broker on `8142`
- compiler readiness is held until local PuppetDB has completed a successful sync and remains within the configured max sync age

## Workload Shape

The current chart is split into:

- `pe` `StatefulSet`
- optional `compiler` `StatefulSet`
- `compiler-signer` Job
- `classifier-config` Job

The `pe` deployment uses multiple containers from the same image:

- `postgresql`
- `puppetdb`
- `puppetserver`
- `nginx`
- `console-services`
- `orchestration-services`
- `bolt-server`
- `ace-server`
- `host-action-collector`

This keeps one PE service per container while colocating the non-compiler Puppet Server with the tightly-coupled HTTP/API edge services. `service/pe` maps directly to the owning container ports in the pod, and compiler capacity remains separate.

Resource tuning follows the same workload boundary:

- `controlPlane.resources.*` configures the containers in the `pe` pod
- `compilers.resources.*` configures the containers in the compiler pod set

The control-plane `StatefulSet` adds:

- stable pod identity such as `pe-0.pe-headless.<namespace>.svc.cluster.local`
- a headless service for replica addressing
- per-replica PVCs like `etc-pe-0`, `opt-pe-0`, and `runtime-pe-0`
- runtime rendering of `pe.conf` so each replica gets its own certname while still advertising the shared front-door DNS names

## Installer Config Model

The chart renders a `pe.conf` template and the install init container fills in replica-specific values at runtime. The generated config covers:

- `puppet_enterprise::certname`
- `puppet_enterprise::puppet_master_host`
- `pe_install::puppet_master_dnsaltnames`
- `puppet_enterprise::profile::master::dns_alt_names`

Generated SANs always include:

- the release front-door service names
- the replica pod FQDN on the control-plane headless service

They can also add:

- one external technical hostname via `network.technicalHostname`
- one external compiler hostname via `network.compilerHostname`
- extra SANs via `network.additionalDnsAltNames`

For control-plane replicas, the install-time `puppet_enterprise::certname`, `certificate_authority_host`, and `puppet_master_host` default to the replica FQDN on the control-plane headless service. That ensures PE installs the local CA, master, console, PuppetDB, and database roles onto the owning pod instead of mistaking the shared service name for another node.

The shared `service/pe` name remains an external front door:

- it is still included in the control-plane certificate SAN set
- Helm Jobs and compiler bootstrap flows can still target it explicitly
- ingress and service routing still present that shared address to clients

You can still override `peConfig.certname` explicitly for a single control-plane replica, but the default is the replica FQDN on the control-plane headless service.

For settings that are not modeled yet, append raw installer config with `peConfig.extra`.

Naming follows the Helm release name:

- release `pe` renders base resources like `pe`, `pe-compiler`, and `pe-compiler-headless` when compilers are enabled
- release `pe` also renders `pe-headless` and stateful PVC names such as `etc-pe-0`
- release `foo` renders `pe-foo`, `pe-foo-compiler`, and `pe-foo-compiler-headless` when compilers are enabled

## Access Model

The intended access pattern is:

- `service/pe` is the technical front door for control-plane traffic
- when the control plane has more than one replica, `service/pe` can be selector-pinned to one healthy `pe` replica at a time so control-plane traffic sees a stable backend
- selector promotion is gated by Relay-published front-door eligibility rather than Pod `Ready` alone
- ingress points at `service/pe` for the console hostname, with TLS terminated by the ingress controller
- `service/pe-compiler` is the optional compiler-pool endpoint for catalog traffic on `8140` and PCP broker traffic on `8142`
- there are no standalone `service/pe-puppetdb` or `service/pe-postgresql` objects in the current model
- when compilers are enabled, the `classifier-config` Job updates PE's built-in `PE Agent` node group so agent catalogs use the compiler endpoint for `server_list`, `primary_uris`, and `pcp_broker_list`
- compiler-to-compiler coordination is not a replication mechanism in this chart
- the long-term goal is that any healthy control-plane replica behind `service/pe` can satisfy compiler-facing control-plane traffic without stable routing
- if a specific surface temporarily requires routing constraints while convergence work is incomplete, that is a tactical safeguard rather than the target model
- selector-backed `service/pe` is the current stable-backend safeguard: console, compiler file-sync, and orchestration traffic stay pinned to one healthy control-plane replica until those surfaces are replica-safe behind unfettered `service/pe` routing
- each `pe` pod publishes front-door eligibility, blockers, and selector state as pod annotations so the active backend and blocked standbys are visible without reading Relay logs

## Conductor Direction

The active-active direction for this repo is Conductor, not direct PE-to-PE synchronization.

That keeps the core boundary intact:

- PostgreSQL, PuppetDB, classification, RBAC, CA, and orchestration state are locally owned inside one Helm release
- compiler pods remain attached to the control-plane stack inside that same release
- compilers are not asked to replicate management state among themselves
- separate Helm releases are independent development stacks, not replication peers

The Conductor components map onto this runtime model as follows:

- Fabric provides the queue transport and node-local participant identity
- Warden governs membership, onboarding, trust distribution, and key rotation eligibility
- Relay handles Puppet Server to PuppetDB propagation while keeping reads local
- Gateway fronts orchestrator and PCP traffic

The current repo baseline is therefore a prerequisite for Conductor, not the finished HA design. The point of the chart today is to preserve local ownership cleanly enough that Fabric, Relay, Gateway, and Warden can be introduced without shared storage.

That does not mean the repo is committed to a permanent Worker/SPOG split in
Kubernetes. In this project, those Conductor terms are best understood as
logical traffic roles. The preferred end state is still a pooled `service/pe`
front door backed by equivalent `pe` replicas. A SPOG-only topology is only
worth introducing later if it solves an actual operational problem that the
pooled release model cannot solve cleanly.

The current Conductor foundation slice is release-topology-driven. Warden expands stable workload sets inside a release, including the `pe` control-plane `StatefulSet` and the compiler `StatefulSet`, into participant identities and onboarding bundles. That is intentionally different from treating separate Helm releases as static peers.

When `conductor.enabled=true` on the PE chart:

- each `pe` and compiler pod gets a `conductor-participant` sidecar
- the sidecar derives its participant identity from the StatefulSet pod name
- the sidecar fetches its Warden-managed onboarding Secret from the Kubernetes API
- the sidecar joins the Fabric hub as a queue consumer for that pod identity
- control-plane pods publish local `ca.pem` and `crl.pem` material back into the Conductor namespace
- Warden assembles a segment trust bundle from those control-plane trust sources
- participant sidecars install the current trust bundle into a pod-local directory for later Relay and Gateway consumption
- participant readiness can remove a pod from `service/pe` or `service/pe-compiler` when onboarding, Fabric connectivity, or trust-bundle currency falls out of policy

When `conductor.relay.enabled=true` as well:

- each `pe` and compiler pod also gets a `conductor-relay` sidecar from the same Conductor image
- Relay reuses the pod's onboarding Secret, but consumes Fabric on its own durable `relay.<pod>` queue
- Relay polls local PuppetDB status over the pod-local listener and publishes that health view into Fabric
- Puppet Server keeps local PuppetDB `server_urls`, while runtime startup patches `submit_only_server_urls` to the pod-local Relay command proxy
- Relay captures selected submit-only PuppetDB commands from the local Puppet Server and replays those commands to the `control-plane` role through Fabric
- Relay stores fresh peer Relay snapshots in a pod-local directory so later routing and replay logic can reason about peer state without shared storage
- Relay readiness can remove a pod from service when participant trust is stale or the local PuppetDB state is not healthy enough for that pod role
- facts, reports, and deactivate-node commands can now traverse Fabric; full catalogs remain local-only by default

When `conductor.relay.classifierSync.enabled=true`:

- each control-plane relay projects a filtered managed classifier domain from the live tree rooted at `All Nodes`
- `All Environments` and `PE Patch Management` are treated as semantic anchors, so their local installer-created IDs can differ while their contents still converge across replicas
- ordinary user-created node groups under `All Nodes` stay in the replicated domain with stable group IDs
- PE-owned local infrastructure groups such as `PE Infrastructure` stay outside the replicated domain
- relay readiness can fail if that managed classifier domain is stale or not converged for the local replica

When `conductor.relay.rbacSync.enabled=true`:

- control-plane relays share console auth material so locally issued RBAC tokens can validate on peer `pe` replicas
- relay projects the managed RBAC database domain through Fabric and can rehydrate that domain onto peer control-plane replicas from Cassandra-backed shared state
- reserved operator token prefixes such as `pe-k8s-conductor-` stay excluded from the replicated domain so local maintenance tokens are not revoked by convergence
- ephemeral per-replica activity fields such as `last_login` and token `last_active` are intentionally normalized out of the authoritative convergence token
- relay readiness can fail if the RBAC managed domain is stale or not converged for the local replica

When the Cassandra backend is enabled for `rbacSync` and `rbacTokenSync`,
Relay treats Cassandra as the durable shared authority for the managed RBAC
domain, shared auth files, and normal RBAC tokens. Local RBAC PostgreSQL then
acts only as the execution-local projection.

The first slices of that replacement are login-session handoff for the auth
barrier, persisted orchestration inventory, and persisted orchestration job
and plan state. When Cassandra-backed session storage is enabled, Relay can
publish a new local `loginsession` row into Cassandra and another `pe`
replica can recreate that row in its own local RBAC database on demand when
the browser arrives with the session cookie. Relay can now treat
`pe-inventory` plus `inventoryKeysJson` and the managed `pe-orchestrator`
database plus `orchestratorEncryptionStore` the same way, with shared state
in Cassandra and local PostgreSQL used only as the execution-local
projection.

Relay is no longer limited to the PuppetDB submit-only path. The current implementation also converges the managed orchestration database domain between control-plane replicas, while Gateway and selector-backed `service/pe` routing keep PCP broker ownership and Bolt/orchestrator client traffic on one healthy control-plane replica at a time.

With the Cassandra backend enabled for orchestration and inventory sync,
local PE PostgreSQL is likewise an execution-local cache or projection rather
than peer-replayed authority.

That gives the release a real Fabric membership model without shared storage or hard-coded peer lists, and live validation now covers Bolt task execution, plan execution, and selector failover from one control-plane replica to the other. It still does not mean the release is finished as a fully pooled active-active PE control plane.

The repo now also carries explicit operator validation helpers:

- `scripts/pe-frontdoor-status.sh` prints the selected `service/pe` backend and the per-pod front-door annotations
- `scripts/validate-pe-failover.sh` runs a live failover exercise against the current release by checking the login page, code deploy, orchestration task/plan execution, and agent catalog flow before and after deleting the selected `pe` pod

CA, classification, code-deploy intent, RBAC/local-auth, and managed orchestration job state are now in place, but the browser console, PCP/orchestration path, and compiler file-sync path are intentionally treated as stable-backend traffic through selector-backed `service/pe`. Current evidence indicates that `pe-inventory` backs saved connection inventory such as `/connections`, `/query`, and `/overwrite-connections`, not live PCP broker presence, so an empty `pe-inventory` database during certname-driven task and plan validation is expected. The open orchestration question is broader PCP mediation and any additional inventory surfaces that should converge beyond those saved connection records.

The larger architectural direction is now to prefer Cassandra-backed
Conductor shared state for the remaining replicated control-plane domains
while preserving the existing `service/pe` and `service/pe-compiler`
deployment shape. Peer PostgreSQL replay remains only as a fallback backend,
not the preferred HA path.

## Code Manager

Code Manager can be modeled directly from Helm values:

- set `codeManager.enabled=true`
- set `codeManager.r10kRemote`
- provide `codeManager.r10kKnownHosts`
- provide the deploy key with an existing Kubernetes Secret via `codeManager.r10kPrivateKeySecretName`

The chart renders these Code Manager `pe.conf` settings when enabled:

- `puppet_enterprise::profile::master::code_manager_auto_configure`
- `puppet_enterprise::profile::master::r10k_remote`
- `puppet_enterprise::profile::master::r10k_private_key`
- `puppet_enterprise::profile::master::r10k_known_hosts`
- `puppet_enterprise::profile::master::r10k_remote_timeout`

The deploy key is mounted from the Secret into the `pe` install init container and the `puppetserver` container at `codeManager.r10kPrivateKeyPath`. For an existing release, set `installer.forceReinstall=true` for the upgrade that introduces or materially changes Code Manager configuration so PE re-runs `puppet infrastructure configure`.

If the Git remote hostname needs a Kubernetes-specific override, set `network.hostAliases`. This is useful when the SSH endpoint for the control repo resolves differently inside the cluster than it does on an operator workstation.

Across multiple Workers, the operator entrypoint should remain Code Manager or r10k. The active-active extension is for Fabric to carry signed deploy intent and convergence state so that each Worker still performs its own local Code Manager deploy for the exact requested revision. That keeps code rollout PE-native on each Worker while avoiding direct PE-to-PE synchronization.

## Validation Agents

For development testing, a separate `puppet-agent` chart can create persistent Kubernetes-backed test nodes and render a signer Job that signs those node certificates against the in-cluster PE CA.

The PE chart does not model validation-agent behavior. Certificate signing for that path lives entirely in the separate `puppet-agent` chart.

The separate validation-node chart lives at `charts/puppet-agent/`. It creates a StatefulSet-backed agent node that:

- installs `puppet-agent` from the PE package repo at runtime
- bootstraps SSL automatically against the PE CA
- runs one immediate `puppet agent -t`
- stays up as a long-running agent so you can re-test code deployment and reporting later

Default behavior:

- release `test-node` creates pod `test-node-puppet-agent-0`
- the default certname becomes `test-node-puppet-agent-0.test.puppet`
- the default PE package repo is fetched from the CA endpoint, usually `https://pe:8140/packages/current/el-9-x86_64.repo`
- the default PE server is `pe` and the default CA is also `pe`
- when `signer.enabled=true`, the chart also runs a signer Job that watches for the expected pending test-node certificate requests and signs them through the PE CA API

With compilers enabled, the expected steady-state split is:

- `ca_server` remains on `pe`
- catalog traffic can target the compiler endpoint
- PCP broker traffic targets the compiler endpoint on `8142`

This helper path is useful for validating:

- certificate issuance and explicit signing behavior
- catalog compilation through `service/pe-compiler`
- PCP broker connectivity through `service/pe-compiler`
- facts, catalogs, and reports landing in PuppetDB
- Code Manager and control-repo changes from a real agent run

## Important Boundary

This scaffold is intentionally Kubernetes-first, but not yet production-ready.

Open design work remains around:

- release-internal replication of PE-local management state across multiple control-plane replicas
- service boundaries inside `/opt/puppetlabs/server/data/*`
- upgrade orchestration
- ownership and security hardening
- secrets and certificate rotation

One specific gap used to be that the `pe` workload had no stable per-replica identity or storage. That gap is now closed at the chart/runtime layer: `pe` is a StatefulSet with per-replica PVCs and runtime-rendered identity. Another recent gap was release-internal Fabric membership and trust distribution; that is now present through the optional `conductor-participant` sidecars, Warden-assembled trust bundles, and trust-aware participant readiness. The remaining gap is active-active synchronization of PE-owned state across the control-plane replicas themselves.

The mapping doc [legacy-service-mapping.md](legacy-service-mapping.md) is the source of truth for the next decomposition steps.

## Certificate Maintenance

Primary certificate regeneration and recovery are not modeled by the current single-owner chart.

That work needs an in-pod maintenance path that operates against the PE-owned state without reintroducing shared PVC mounts from helper Jobs. Until that exists, treat SAN changes and primary certificate recovery as manual operator procedures rather than Helm features.
