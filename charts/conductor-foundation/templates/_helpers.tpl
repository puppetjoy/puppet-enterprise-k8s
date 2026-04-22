{{- define "conductor.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "conductor.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- if eq .Release.Name "conductor" -}}
conductor
{{- else if hasPrefix "conductor-" .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "conductor-%s" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "conductor.labels" -}}
app.kubernetes.io/name: {{ include "conductor.name" . }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "conductor.foundationMode" -}}
{{- default "auto" .Values.foundation.mode -}}
{{- end -}}

{{- define "conductor.foundationEnabled" -}}
{{- $mode := include "conductor.foundationMode" . -}}
{{- if eq $mode "enabled" -}}
true
{{- else if eq $mode "disabled" -}}
false
{{- else if and (gt (int .Values.topology.controlPlaneReplicaCount) 1) (gt (int .Values.topology.compilerReplicaCount) 0) -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "conductor.wardenEnabled" -}}
{{- if and (eq (include "conductor.foundationEnabled" .) "true") .Values.warden.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "conductor.cassandraEnabled" -}}
{{- if and (eq (include "conductor.foundationEnabled" .) "true") .Values.cassandra.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "conductor.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "conductor.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "conductor.hubServiceName" -}}
{{- printf "%s-hub" (include "conductor.fullname" .) -}}
{{- end -}}

{{- define "conductor.hubHeadlessServiceName" -}}
{{- printf "%s-headless" (include "conductor.hubServiceName" .) -}}
{{- end -}}

{{- define "conductor.hubAuthSecretName" -}}
{{- if .Values.hub.auth.existingSecretName -}}
{{- .Values.hub.auth.existingSecretName -}}
{{- else -}}
{{- printf "%s-hub-auth" (include "conductor.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "conductor.wardenConfigMapName" -}}
{{- printf "%s-warden-config" (include "conductor.fullname" .) -}}
{{- end -}}

{{- define "conductor.cassandraServiceName" -}}
{{- printf "%s-cassandra" (include "conductor.fullname" .) -}}
{{- end -}}

{{- define "conductor.cassandraHeadlessServiceName" -}}
{{- printf "%s-headless" (include "conductor.cassandraServiceName" .) -}}
{{- end -}}
