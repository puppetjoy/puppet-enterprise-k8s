{{- define "puppet-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "puppet-agent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "puppet-agent.labels" -}}
app.kubernetes.io/name: {{ include "puppet-agent.name" . }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "puppet-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "puppet-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "puppet-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "puppet-agent.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "puppet-agent.peReleaseFullname" -}}
{{- $instance := default "pe" .Values.signer.peReleaseName -}}
{{- if eq $instance "pe" -}}
pe
{{- else if hasPrefix "pe-" $instance -}}
{{- $instance -}}
{{- else -}}
{{- printf "pe-%s" $instance | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "puppet-agent.caServer" -}}
{{- if .Values.agent.caServer -}}
{{- .Values.agent.caServer -}}
{{- else -}}
{{- $pe := include "puppet-agent.peReleaseFullname" . -}}
{{- printf "%s-ca" $pe -}}
{{- end -}}
{{- end -}}

{{- define "puppet-agent.certnameForIndex" -}}
{{- if .Values.agent.certname -}}
{{- .Values.agent.certname -}}
{{- else -}}
{{- printf "%s-%d%s" (include "puppet-agent.fullname" .) (int .index) .Values.agent.certnameSuffix -}}
{{- end -}}
{{- end -}}

{{- define "puppet-agent.certnamesCsv" -}}
{{- if .Values.agent.certname -}}
{{- .Values.agent.certname -}}
{{- else -}}
{{- range $index, $_ := until (int $.Values.agent.replicaCount) -}}
{{- if gt $index 0 }},{{ end -}}
{{ include "puppet-agent.certnameForIndex" (dict "Values" $.Values "Release" $.Release "Chart" $.Chart "index" $index) }}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "puppet-agent.packageRepoHost" -}}
{{- if .Values.agent.packageRepoServer -}}
{{- .Values.agent.packageRepoServer -}}
{{- else -}}
{{- include "puppet-agent.caServer" . -}}
{{- end -}}
{{- end -}}

{{- define "puppet-agent.packageRepoUrl" -}}
{{- if .Values.agent.packageRepoUrl -}}
{{- .Values.agent.packageRepoUrl -}}
{{- else -}}
{{- printf "https://%s:8140/packages/current/el-9-x86_64.repo" (include "puppet-agent.packageRepoHost" .) -}}
{{- end -}}
{{- end -}}
