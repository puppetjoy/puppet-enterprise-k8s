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
