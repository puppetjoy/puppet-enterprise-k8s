{{- define "pe.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "pe.instance" -}}
{{- default .Release.Name .Values.instanceOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "pe.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $instance := include "pe.instance" . -}}
{{- if eq $instance "pe" -}}
pe
{{- else if hasPrefix "pe-" $instance -}}
{{- $instance -}}
{{- else -}}
{{- printf "pe-%s" $instance | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "pe.identity" -}}
{{- include "pe.fullname" . -}}
{{- end -}}

{{- define "pe.certname" -}}
{{- default (include "pe.identity" .) .Values.peConfig.certname -}}
{{- end -}}

{{- define "pe.puppetMasterHost" -}}
{{- default (include "pe.identity" .) .Values.peConfig.puppetMasterHost -}}
{{- end -}}

{{- define "pe.dnsAltNames" -}}
{{- $identity := include "pe.identity" . -}}
{{- $certname := include "pe.certname" . -}}
{{- $puppetMasterHost := include "pe.puppetMasterHost" . -}}
{{- $internal := list
    $certname
    $puppetMasterHost
    $identity
    (printf "%s.%s" $identity .Release.Namespace)
    (printf "%s.%s.svc" $identity .Release.Namespace)
    (printf "%s.%s.svc.cluster.local" $identity .Release.Namespace)
-}}
{{- $external := list -}}
{{- if .Values.network.technicalHostname -}}
{{- $external = append $external .Values.network.technicalHostname -}}
{{- end -}}
{{- $additional := default (list) .Values.network.additionalDnsAltNames -}}
{{- $dnsAltNames := concat $internal $external $additional | uniq -}}
{{- range $dnsAltNames }}
- {{ . | quote }}
{{- end -}}
{{- end -}}

{{- define "pe.dnsAltNamesHocon" -}}
{{- $identity := include "pe.identity" . -}}
{{- $certname := include "pe.certname" . -}}
{{- $puppetMasterHost := include "pe.puppetMasterHost" . -}}
{{- $internal := list
    $certname
    $puppetMasterHost
    $identity
    (printf "%s.%s" $identity .Release.Namespace)
    (printf "%s.%s.svc" $identity .Release.Namespace)
    (printf "%s.%s.svc.cluster.local" $identity .Release.Namespace)
-}}
{{- $external := list -}}
{{- if .Values.network.technicalHostname -}}
{{- $external = append $external .Values.network.technicalHostname -}}
{{- end -}}
{{- $additional := default (list) .Values.network.additionalDnsAltNames -}}
{{- $dnsAltNames := concat $internal $external $additional | uniq -}}
[
{{- range $index, $name := $dnsAltNames }}
  {{- if gt $index 0 }},{{ end }}
  {{ $name | quote }}
{{- end }}
]
{{- end -}}

{{- define "pe.dnsAltNamesCsv" -}}
{{- $identity := include "pe.identity" . -}}
{{- $certname := include "pe.certname" . -}}
{{- $puppetMasterHost := include "pe.puppetMasterHost" . -}}
{{- $internal := list
    $certname
    $puppetMasterHost
    $identity
    (printf "%s.%s" $identity .Release.Namespace)
    (printf "%s.%s.svc" $identity .Release.Namespace)
    (printf "%s.%s.svc.cluster.local" $identity .Release.Namespace)
-}}
{{- $external := list -}}
{{- if .Values.network.technicalHostname -}}
{{- $external = append $external .Values.network.technicalHostname -}}
{{- end -}}
{{- $additional := default (list) .Values.network.additionalDnsAltNames -}}
{{- $dnsAltNames := concat $internal $external $additional | uniq -}}
{{ join "," $dnsAltNames }}
{{- end -}}

{{- define "pe.generatedPeConf" -}}
"console_admin_password" = {{ .Values.peConfig.consoleAdminPassword | quote }}
"puppet_enterprise::puppet_master_host" = {{ include "pe.puppetMasterHost" . | quote }}
"puppet_enterprise::certname" = {{ include "pe.certname" . | quote }}
"puppet_enterprise::profile::master::dns_alt_names" = {{ include "pe.dnsAltNamesHocon" . | trim }}
{{- if .Values.codeManager.enabled }}
"puppet_enterprise::profile::master::code_manager_auto_configure" = {{ .Values.codeManager.autoConfigure }}
"puppet_enterprise::profile::master::r10k_remote" = {{ required "codeManager.r10kRemote is required when codeManager.enabled=true" .Values.codeManager.r10kRemote | quote }}
"puppet_enterprise::profile::master::r10k_private_key" = {{ required "codeManager.r10kPrivateKeyPath is required when codeManager.enabled=true" .Values.codeManager.r10kPrivateKeyPath | quote }}
{{- if .Values.codeManager.r10kKnownHosts }}
"puppet_enterprise::profile::master::r10k_known_hosts" = {{ include "pe.codeManagerKnownHostsHocon" . | trim }}
{{- end }}
"puppet_enterprise::profile::master::r10k_remote_timeout" = {{ .Values.codeManager.r10kRemoteTimeout }}
{{- end }}
{{- if and .Values.compilers.enabled (gt (int .Values.compilers.replicaCount) 0) }}
"puppet_enterprise::master::file_sync::allowlisted_certnames" = {{ include "pe.compilerCertnamesHocon" . | trim }}
"puppet_enterprise::profile::console::allowlisted_certnames" = {{ include "pe.compilerCertnamesHocon" . | trim }}
"puppet_enterprise::profile::database::private_temp_puppetdb_hosts" = {{ include "pe.compilerCertnamesHocon" . | trim }}
"puppet_enterprise::profile::master::provisioned_replicas" = {{ include "pe.compilerCertnamesHocon" . | trim }}
"puppet_enterprise::profile::puppetdb::allowlisted_certnames" = {{ include "pe.compilerCertnamesHocon" . | trim }}
{{- end }}
{{- with .Values.peConfig.extra }}

{{ . | trim }}
{{- end -}}
{{- end -}}

{{- define "pe.codeManagerKnownHostsHocon" -}}
[
{{- range $index, $entry := .Values.codeManager.r10kKnownHosts }}
  {{- if gt $index 0 }},{{ end }}
  {"name": {{ required "codeManager.r10kKnownHosts[].name is required" $entry.name | quote }}, "type": {{ required "codeManager.r10kKnownHosts[].type is required" $entry.type | quote }}, "key": {{ required "codeManager.r10kKnownHosts[].key is required" $entry.key | quote }}}
{{- end }}
]
{{- end -}}

{{- define "pe.compilerStatefulSetName" -}}
{{- printf "%s-compiler" (include "pe.fullname" .) -}}
{{- end -}}

{{- define "pe.compilerHeadlessServiceName" -}}
{{- printf "%s-headless" (include "pe.compilerStatefulSetName" .) -}}
{{- end -}}

{{- define "pe.compilerPoolServiceName" -}}
{{- include "pe.compilerStatefulSetName" . -}}
{{- end -}}

{{- define "pe.compilerPodNameForIndex" -}}
{{- $root := .root -}}
{{- $index := int .index -}}
{{- printf "%s-%d" (include "pe.compilerStatefulSetName" $root) $index -}}
{{- end -}}

{{- define "pe.compilerCertnameForIndex" -}}
{{- $root := .root -}}
{{- $podName := include "pe.compilerPodNameForIndex" . -}}
{{- printf "%s.%s.%s.svc.cluster.local" $podName (include "pe.compilerHeadlessServiceName" $root) $root.Release.Namespace -}}
{{- end -}}

{{- define "pe.compilerPoolDnsNames" -}}
{{- $service := include "pe.compilerPoolServiceName" . -}}
{{- $internal := list
    $service
    (printf "%s.%s" $service .Release.Namespace)
    (printf "%s.%s.svc" $service .Release.Namespace)
    (printf "%s.%s.svc.cluster.local" $service .Release.Namespace)
-}}
{{- $additional := default (list) .Values.compilers.dnsAltNames -}}
{{- $dnsAltNames := concat $internal $additional | uniq -}}
{{- range $dnsAltNames }}
- {{ . | quote }}
{{- end -}}
{{- end -}}

{{- define "pe.compilerPoolDnsNamesCsv" -}}
{{- $service := include "pe.compilerPoolServiceName" . -}}
{{- $internal := list
    $service
    (printf "%s.%s" $service .Release.Namespace)
    (printf "%s.%s.svc" $service .Release.Namespace)
    (printf "%s.%s.svc.cluster.local" $service .Release.Namespace)
-}}
{{- $additional := default (list) .Values.compilers.dnsAltNames -}}
{{- $dnsAltNames := concat $internal $additional | uniq -}}
{{ join "," $dnsAltNames }}
{{- end -}}

{{- define "pe.compilerCertnamesHocon" -}}
[
{{- range $index, $_ := until (int .Values.compilers.replicaCount) }}
  {{- if gt $index 0 }},{{ end }}
  {{ include "pe.compilerCertnameForIndex" (dict "root" $ "index" $index) | quote }}
{{- end }}
]
{{- end -}}

{{- define "pe.compilerCertnamesCsv" -}}
{{- range $index, $_ := until (int .Values.compilers.replicaCount) -}}
{{- if gt $index 0 }},{{ end -}}
{{ include "pe.compilerCertnameForIndex" (dict "root" $ "index" $index) }}
{{- end -}}
{{- end -}}

{{- define "pe.labels" -}}
app.kubernetes.io/name: {{ include "pe.name" . }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "pe.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "pe.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
