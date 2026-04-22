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

{{- define "pe.controlPlaneStatefulSetName" -}}
{{- include "pe.fullname" . -}}
{{- end -}}

{{- define "pe.controlPlaneHeadlessServiceName" -}}
{{- printf "%s-headless" (include "pe.controlPlaneStatefulSetName" .) -}}
{{- end -}}

{{- define "pe.compilerPoolEnabled" -}}
{{- if and .Values.compilers.enabled (gt (int .Values.compilers.replicaCount) 0) -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.replicatedControlPlaneEnabled" -}}
{{- if gt (int .Values.controlPlane.replicaCount) 1 -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorMode" -}}
{{- default "auto" .Values.conductor.mode -}}
{{- end -}}

{{- define "pe.conductorEnabled" -}}
{{- $mode := include "pe.conductorMode" . -}}
{{- if eq $mode "enabled" -}}
true
{{- else if eq $mode "disabled" -}}
false
{{- else if eq $mode "manual" -}}
{{- ternary "true" "false" .Values.conductor.enabled -}}
{{- else if eq (include "pe.replicatedControlPlaneEnabled" .) "true" -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayEnabled" -}}
{{- if and (eq (include "pe.conductorEnabled" .) "true") .Values.conductor.relay.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorGatewayEnabled" -}}
{{- if and (eq (include "pe.conductorEnabled" .) "true") .Values.conductor.gateway.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayCommandProxyEnabled" -}}
{{- if and (eq (include "pe.conductorRelayEnabled" .) "true") .Values.conductor.relay.commandProxy.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayCodeDeployEnabled" -}}
{{- if and (eq (include "pe.conductorRelayEnabled" .) "true") .Values.conductor.relay.codeDeploy.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayClassifierSyncEnabled" -}}
{{- if and (eq (include "pe.conductorRelayEnabled" .) "true") .Values.conductor.relay.classifierSync.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayRbacSyncEnabled" -}}
{{- if and (eq (include "pe.conductorRelayEnabled" .) "true") .Values.conductor.relay.rbacSync.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayRbacTokenSyncEnabled" -}}
{{- if and (eq (include "pe.conductorRelayEnabled" .) "true") .Values.conductor.relay.rbacTokenSync.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayOrchestrationSyncEnabled" -}}
{{- if and (eq (include "pe.conductorRelayEnabled" .) "true") .Values.conductor.relay.orchestrationSync.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayInventorySyncEnabled" -}}
{{- if and (eq (include "pe.conductorRelayEnabled" .) "true") .Values.conductor.relay.inventorySync.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayCaSyncEnabled" -}}
{{- if and (eq (include "pe.conductorRelayEnabled" .) "true") .Values.conductor.relay.caSync.enabled -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.conductorRelayWritesEtc" -}}
{{- if or
    (eq (include "pe.conductorRelayCaSyncEnabled" .) "true")
    (eq (include "pe.conductorRelayRbacSyncEnabled" .) "true")
    (eq (include "pe.conductorRelayRbacTokenSyncEnabled" .) "true")
    (eq (include "pe.conductorRelayOrchestrationSyncEnabled" .) "true")
    (eq (include "pe.conductorRelayInventorySyncEnabled" .) "true")
-}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.frontDoorSelectorEnabled" -}}
{{- if and (eq (include "pe.replicatedControlPlaneEnabled" .) "true") (eq (include "pe.conductorEnabled" .) "true") -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{- define "pe.sanitizeFragment" -}}
{{- $clean := regexReplaceAll "[^a-z0-9-]+" (lower (toString .)) "-" -}}
{{- $clean = trimAll "-" $clean -}}
{{- if $clean -}}
{{- $clean -}}
{{- else -}}
default
{{- end -}}
{{- end -}}

{{- define "pe.conductorTrustBundleSecretName" -}}
{{- $prefix := include "pe.sanitizeFragment" .Values.conductor.resourcePrefix -}}
{{- $segment := include "pe.sanitizeFragment" .Values.conductor.segmentName -}}
{{- printf "%s-%s-trust-bundle" $prefix $segment | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "pe.controlPlanePodNameForIndex" -}}
{{- $root := .root -}}
{{- $index := int .index -}}
{{- printf "%s-%d" (include "pe.controlPlaneStatefulSetName" $root) $index -}}
{{- end -}}

{{- define "pe.controlPlaneCaSecretNameForIndex" -}}
{{- printf "%s-ca" (include "pe.controlPlanePodNameForIndex" .) -}}
{{- end -}}

{{- define "pe.controlPlaneRootCaSecretName" -}}
{{- default (printf "%s-control-plane-root-ca" (include "pe.fullname" .)) .Values.controlPlane.ca.releaseRoot.rootSecretName -}}
{{- end -}}

{{- define "pe.controlPlaneCaBundleSecretName" -}}
{{- default (printf "%s-control-plane-ca-bundle" (include "pe.fullname" .)) .Values.controlPlane.ca.releaseRoot.bundleSecretName -}}
{{- end -}}

{{- define "pe.controlPlaneDefaultCertnameForIndex" -}}
{{- $root := .root -}}
{{- $podName := include "pe.controlPlanePodNameForIndex" . -}}
{{- printf "%s.%s.%s.svc.cluster.local" $podName (include "pe.controlPlaneHeadlessServiceName" $root) $root.Release.Namespace -}}
{{- end -}}

{{- define "pe.certname" -}}
{{- if .Values.peConfig.certname -}}
{{- .Values.peConfig.certname -}}
{{- else -}}
{{- include "pe.controlPlaneDefaultCertnameForIndex" (dict "root" . "index" 0) -}}
{{- end -}}
{{- end -}}

{{- define "pe.puppetMasterHost" -}}
{{- default (include "pe.identity" .) .Values.peConfig.puppetMasterHost -}}
{{- end -}}

{{- define "pe.controlPlaneFrontDoorDnsNames" -}}
{{- $identity := include "pe.identity" . -}}
{{- $puppetMasterHost := include "pe.puppetMasterHost" . -}}
{{- $serviceNames := list
    $identity
    (printf "%s.%s" $identity .Release.Namespace)
    (printf "%s.%s.svc" $identity .Release.Namespace)
    (printf "%s.%s.svc.cluster.local" $identity .Release.Namespace)
-}}
{{- $frontDoorNames := list $puppetMasterHost -}}
{{- $external := list -}}
{{- if .Values.peConfig.certname -}}
{{- $external = append $external .Values.peConfig.certname -}}
{{- end -}}
{{- if .Values.network.technicalHostname -}}
{{- $external = append $external .Values.network.technicalHostname -}}
{{- end -}}
{{- $additional := default (list) .Values.network.additionalDnsAltNames -}}
{{- $dnsAltNames := concat $serviceNames $frontDoorNames $external $additional | uniq -}}
{{- range $dnsAltNames }}
- {{ . | quote }}
{{- end -}}
{{- end -}}

{{- define "pe.controlPlaneFrontDoorDnsNamesHocon" -}}
{{- $identity := include "pe.identity" . -}}
{{- $puppetMasterHost := include "pe.puppetMasterHost" . -}}
{{- $serviceNames := list
    $identity
    (printf "%s.%s" $identity .Release.Namespace)
    (printf "%s.%s.svc" $identity .Release.Namespace)
    (printf "%s.%s.svc.cluster.local" $identity .Release.Namespace)
-}}
{{- $frontDoorNames := list $puppetMasterHost -}}
{{- $external := list -}}
{{- if .Values.peConfig.certname -}}
{{- $external = append $external .Values.peConfig.certname -}}
{{- end -}}
{{- if .Values.network.technicalHostname -}}
{{- $external = append $external .Values.network.technicalHostname -}}
{{- end -}}
{{- $additional := default (list) .Values.network.additionalDnsAltNames -}}
{{- $dnsAltNames := concat $serviceNames $frontDoorNames $external $additional | uniq -}}
[
{{- range $index, $name := $dnsAltNames }}
  {{- if gt $index 0 }},{{ end }}
  {{ $name | quote }}
{{- end }}
]
{{- end -}}

{{- define "pe.controlPlaneFrontDoorDnsNamesCsv" -}}
{{- $identity := include "pe.identity" . -}}
{{- $puppetMasterHost := include "pe.puppetMasterHost" . -}}
{{- $serviceNames := list
    $identity
    (printf "%s.%s" $identity .Release.Namespace)
    (printf "%s.%s.svc" $identity .Release.Namespace)
    (printf "%s.%s.svc.cluster.local" $identity .Release.Namespace)
-}}
{{- $frontDoorNames := list $puppetMasterHost -}}
{{- $external := list -}}
{{- if .Values.peConfig.certname -}}
{{- $external = append $external .Values.peConfig.certname -}}
{{- end -}}
{{- if .Values.network.technicalHostname -}}
{{- $external = append $external .Values.network.technicalHostname -}}
{{- end -}}
{{- $additional := default (list) .Values.network.additionalDnsAltNames -}}
{{- $dnsAltNames := concat $serviceNames $frontDoorNames $external $additional | uniq -}}
{{ join "," $dnsAltNames }}
{{- end -}}

{{- define "pe.controlPlaneLoopbackAliases" -}}
{{- $identity := include "pe.identity" . -}}
{{- $aliases := list
    $identity
    (printf "%s.%s" $identity .Release.Namespace)
    (printf "%s.%s.svc" $identity .Release.Namespace)
    (printf "%s.%s.svc.cluster.local" $identity .Release.Namespace)
-}}
{{- $additional := default (list) .Values.network.podLoopbackAliases -}}
{{- $aliases = concat $aliases $additional | uniq -}}
{{ toYaml $aliases }}
{{- end -}}

{{- define "pe.controlPlaneCertnamesHocon" -}}
[
{{- if .Values.peConfig.certname }}
  {{ .Values.peConfig.certname | quote }}
{{- else }}
{{- range $index, $_ := until (int .Values.controlPlane.replicaCount) }}
  {{- if gt $index 0 }},{{ end }}
  {{ include "pe.controlPlaneDefaultCertnameForIndex" (dict "root" $ "index" $index) | quote }}
{{- end }}
{{- end }}
]
{{- end -}}

{{- define "pe.controlPlaneCertnamesCsv" -}}
{{- if .Values.peConfig.certname -}}
{{- .Values.peConfig.certname -}}
{{- else -}}
{{- range $index, $_ := until (int .Values.controlPlane.replicaCount) -}}
{{- if gt $index 0 }},{{ end -}}
{{ include "pe.controlPlaneDefaultCertnameForIndex" (dict "root" $ "index" $index) }}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "pe.generatedPeConf" -}}
"console_admin_password" = {{ .Values.peConfig.consoleAdminPassword | quote }}
"puppet_enterprise::certificate_authority_host" = "__PE_CERTIFICATE_AUTHORITY_HOST__"
"puppet_enterprise::puppet_master_host" = "__PE_PUPPET_MASTER_HOST__"
"puppet_enterprise::certname" = "__PE_CERTNAME__"
"pe_install::puppet_master_dnsaltnames" = __PE_DNS_ALT_NAMES_HOCON__
"puppet_enterprise::profile::master::dns_alt_names" = __PE_DNS_ALT_NAMES_HOCON__
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

{{- define "pe.pvcEtcName" -}}
{{- printf "etc-%s" (include "pe.fullname" .) -}}
{{- end -}}

{{- define "pe.pvcOptName" -}}
{{- printf "opt-%s" (include "pe.fullname" .) -}}
{{- end -}}

{{- define "pe.pvcRuntimeName" -}}
{{- printf "runtime-%s" (include "pe.fullname" .) -}}
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
{{- $external := list -}}
{{- if .Values.network.compilerHostname -}}
{{- $external = append $external .Values.network.compilerHostname -}}
{{- end -}}
{{- $additional := default (list) .Values.compilers.dnsAltNames -}}
{{- $dnsAltNames := concat $internal $external $additional | uniq -}}
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
{{- $external := list -}}
{{- if .Values.network.compilerHostname -}}
{{- $external = append $external .Values.network.compilerHostname -}}
{{- end -}}
{{- $additional := default (list) .Values.compilers.dnsAltNames -}}
{{- $dnsAltNames := concat $internal $external $additional | uniq -}}
{{ join "," $dnsAltNames }}
{{- end -}}

{{- define "pe.compilerPcpBrokerHost" -}}
{{- default (include "pe.compilerPoolServiceName" .) .Values.compilers.pcpBrokerHost -}}
{{- end -}}

{{- define "pe.compilerAgentHost" -}}
{{- default (include "pe.compilerPoolServiceName" .) .Values.network.compilerHostname -}}
{{- end -}}

{{- define "pe.compilerAgentServerListEntry" -}}
{{- printf "%s:%d" (include "pe.compilerAgentHost" .) (int .Values.services.compilers.port) -}}
{{- end -}}

{{- define "pe.compilerAgentPrimaryUri" -}}
{{- printf "https://%s:%d" (include "pe.compilerAgentHost" .) (int .Values.services.compilers.port) -}}
{{- end -}}

{{- define "pe.compilerAgentPcpBrokerEntry" -}}
{{- printf "%s:%d" (include "pe.compilerAgentHost" .) (int .Values.services.compilers.pcpPort) -}}
{{- end -}}

{{- define "pe.orchestratorPcpBrokerCertnamesCsv" -}}
{{- $certnames := list -}}
{{- if .Values.peConfig.certname -}}
{{- $certnames = append $certnames .Values.peConfig.certname -}}
{{- else -}}
{{- range $index, $_ := until (int .Values.controlPlane.replicaCount) -}}
{{- $certnames = append $certnames (include "pe.controlPlaneDefaultCertnameForIndex" (dict "root" $ "index" $index)) -}}
{{- end -}}
{{- end -}}
{{- range $index, $_ := until (int .Values.compilers.replicaCount) -}}
{{- $certnames = append $certnames (include "pe.compilerCertnameForIndex" (dict "root" $ "index" $index)) -}}
{{- end -}}
{{ join "," ($certnames | uniq) }}
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

{{- define "pe.conductorNamespace" -}}
{{- default .Release.Namespace .Values.conductor.namespace -}}
{{- end -}}

{{- define "pe.conductorCassandraServiceName" -}}
{{- default "conductor-cassandra" .Values.conductor.sharedState.cassandra.serviceName -}}
{{- end -}}

{{- define "pe.conductorCassandraDnsName" -}}
{{- printf "%s.%s.svc.cluster.local" (include "pe.conductorCassandraServiceName" .) (include "pe.conductorNamespace" .) -}}
{{- end -}}

{{- define "pe.conductorSharedStateContactPointsCsv" -}}
{{- $root := .root -}}
{{- $points := default (list) .points -}}
{{- if gt (len $points) 0 -}}
{{- join "," $points -}}
{{- else -}}
{{- include "pe.conductorCassandraDnsName" $root -}}
{{- end -}}
{{- end -}}
