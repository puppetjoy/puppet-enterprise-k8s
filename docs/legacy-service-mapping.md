# Legacy PE Service To Kubernetes Mapping

This mapping is based on the legacy PE container work in `../pe-container`.

## Active PE Services

The legacy install currently runs these PE services:

| Service | Listener(s) | Legacy ExecStart | Mutable config/data | Suggested K8s shape | Scaling notes |
| --- | --- | --- | --- | --- | --- |
| `pe-postgresql` | `5432` | `postgres -D /opt/puppetlabs/server/data/postgresql/14/data` | `/opt/puppetlabs/server/data/postgresql`, `/var/log/puppetlabs/postgresql` | Container in `pe` `Deployment` | Single-owner data service behind `service/pe` |
| `pe-puppetdb` | `8081` | `puppetdb foreground` | `/etc/puppetlabs/puppetdb`, `/opt/puppetlabs/server/data/puppetdb` | Container in `pe` `Deployment` | Single-owner data service behind `service/pe` |
| `pe-puppetserver` | `8140`, `8170` | `puppetserver foreground` | `/etc/puppetlabs/puppetserver`, `/opt/puppetlabs/server/data/puppetserver`, code data | Container in `pe` `Deployment` | `service/pe` serves the non-compiler Puppet Server; compiler capacity scales separately |
| `pe-nginx` | `80`, `443` | `nginx -c /etc/puppetlabs/nginx/nginx.conf` | `/etc/puppetlabs/nginx` | `Deployment` container in `pe` | HTTP/TLS edge |
| `pe-console-services` | `4433` | `console-services foreground` | `/etc/puppetlabs/console-services`, `/opt/puppetlabs/server/data/console-services` | `Deployment` container in `pe` | Depends on DB and Puppet Server |
| `pe-orchestration-services` | `8142`, `8143` | `orchestration-services foreground` | `/etc/puppetlabs/orchestration-services`, `/opt/puppetlabs/server/data/orchestration-services` | `Deployment` container in `pe` | Depends on DB |
| `pe-host-action-collector` | `8147` | `host-action-collector foreground` | `/etc/puppetlabs/host-action-collector`, `/opt/puppetlabs/server/data/host-action-collector` | `Deployment` container in `pe` | Depends on DB |
| `pe-bolt-server` | `62658` | `puma -C .../pe_bolt_server_config.rb` | `/etc/puppetlabs/bolt-server`, `/opt/puppetlabs/server/data/bolt-server` | `Deployment` container in `pe` | HTTP API service |
| `pe-ace-server` | `44633` | `puma -C .../transport_tasks_config.rb` | `/etc/puppetlabs/ace-server`, `/opt/puppetlabs/server/data/ace-server` | `Deployment` container in `pe` | HTTP API service |

## Important Paths

Small but critical configuration:

- `/etc/puppetlabs/pe`
- `/etc/puppetlabs/enterprise/conf.d`
- `/etc/puppetlabs/puppet`
- `/etc/puppetlabs/puppetserver`
- `/etc/puppetlabs/puppetdb`
- `/etc/puppetlabs/console-services`
- `/etc/puppetlabs/orchestration-services`
- `/etc/puppetlabs/nginx`

Heavy mutable state under `/opt/puppetlabs/server/data` from the current install:

| Path | Approx size | Notes |
| --- | --- | --- |
| `/opt/puppetlabs/server/data/packages` | `901M` | PE package repo served on `8140` |
| `/opt/puppetlabs/server/data/postgresql` | `66M` | Primary databases |
| `/opt/puppetlabs/server/data/puppetserver` | `9.5M` | JARs, restart state, service-local data |
| `/opt/puppetlabs/server/data/environments` | `2.7M` | Code Manager/filesync managed code |
| `/opt/puppetlabs/server/data/code-manager` | `803K` | Code Manager state |
| `/opt/puppetlabs/server/data/orchestration-services` | `585K` | Orchestrator state |

Export-only rootfs artifacts produced by install:

- `/etc/sysconfig/pe-puppetserver`
- `/etc/sysconfig/pe-puppetdb`
- `/etc/sysconfig/pe-console-services`
- `/etc/sysconfig/pe-orchestration-services`
- `/etc/sysconfig/pe-host-action-collector`
- `/etc/sysconfig/pe-nginx`
- `/etc/sysconfig/pe-pgsql`

These are not under `/etc/puppetlabs`, so the `pe` install init container exports them onto the PE runtime volume for the runtime containers.

## Initial K8s Storage Model

The current scaffold keeps storage single-owner and explicit:

- single-owner RWO PVC for `/etc/puppetlabs` used only by `pe`
- single-owner RWO PVC for `/opt/puppetlabs` used only by `pe`
- single-owner RWO PVC for `/var/lib/pe-k8s` used only by `pe`
- per-compiler non-shared PVCs for `/etc/puppetlabs`, `/opt/puppetlabs`, and `/var/lib/pe-k8s`
- per-pod ephemeral log directories

That model is intentionally conservative. It preserves the official PE install flow while avoiding shared storage between workloads.

## Future Partitioning

The likely next partitioning target is `/opt/puppetlabs/server/data`:

- `postgresql` should eventually have its own PVC
- `packages` may remain shared and possibly read-only after install/update
- `environments` and `code-manager` should line up with file-sync distribution to the compiler pool rather than shared compiler storage
- `puppetserver` and `puppetdb` service-local data should be reviewed for per-pod vs shared semantics

## Why This Is Not A systemd Port

The systemd container proved PE can install and run in a container.
The Kubernetes direction is different:

- no systemd as PID 1
- one PE service per container
- foreground service runners
- init-container install sequencing persisted onto single-owner PVCs
- exported rootfs runtime artifacts restored into each workload pod
