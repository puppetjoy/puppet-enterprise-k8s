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
