{{- define "tsagent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tsagent.fullname" -}}
{{- /* Unchanged from 0.1.0 on purpose: renaming would orphan an existing release's PVC. */ -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "tsagent.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "tsagent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tsagent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tsagent.labels" -}}
{{ include "tsagent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: tsagent
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* metadata.annotations block, only emitted when there is something to put in it. */}}
{{- define "tsagent.annotations" -}}
{{- $all := merge (dict) (.extra | default dict) (.root.Values.commonAnnotations | default dict) -}}
{{- with $all }}
  annotations:
    {{- toYaml . | nindent 4 }}
{{- end }}
{{- end -}}

{{- define "tsagent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "tsagent.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "tsagent.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "tsagent.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* Env var names read from the Secret: secrets.env plus the legacy secrets.apiKeys. */}}
{{- define "tsagent.secretEnv" -}}
{{- toYaml (merge (dict) (.Values.secrets.env | default dict) (.Values.secrets.apiKeys | default dict)) -}}
{{- end -}}

{{- define "tsagent.configMapName" -}}
{{- default (printf "%s-config" (include "tsagent.fullname" .)) .Values.config.existingConfigMap -}}
{{- end -}}
{{- define "tsagent.allowlistConfigMapName" -}}
{{- default (printf "%s-allowlist" (include "tsagent.fullname" .)) .Values.allowlist.existingConfigMap -}}
{{- end -}}
{{- define "tsagent.runbooksConfigMapName" -}}
{{- default (printf "%s-runbooks" (include "tsagent.fullname" .)) .Values.runbooks.existingConfigMap -}}
{{- end -}}
{{- define "tsagent.knowledgeConfigMapName" -}}
{{- default (printf "%s-knowledge" (include "tsagent.fullname" .)) .Values.knowledge.existingConfigMap -}}
{{- end -}}

{{- define "tsagent.imageStreamName" -}}
{{- include "tsagent.fullname" . -}}
{{- end -}}

{{/* The image reference the Deployment runs. */}}
{{- define "tsagent.image" -}}
{{- if .Values.build.enabled -}}
{{- printf "image-registry.openshift-image-registry.svc:5000/%s/%s:%s" .Release.Namespace (include "tsagent.imageStreamName" .) .Values.build.outputTag -}}
{{- else if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "tsagent.httpScheme" -}}
{{- if .Values.tls.serviceServingCert.enabled -}}HTTPS{{- else -}}HTTP{{- end -}}
{{- end -}}

{{- define "tsagent.verifyHostKeys" -}}
{{- if or .Values.secrets.sshKnownHosts .Values.secrets.verifyHostKeys -}}true{{- end -}}
{{- end -}}

{{/*
Rendered settings.yaml: the repo copy (files/config/settings.yaml) with .Values.settings
deep-merged over it.
*/}}
{{- define "tsagent.settingsYaml" -}}
{{- $base := fromYaml (.Files.Get "files/config/settings.yaml") | default dict -}}
{{- toYaml (mergeOverwrite $base (deepCopy (.Values.settings | default dict))) -}}
{{- end -}}

{{/* llm.yaml / clusters.yaml: values replace the repo copy wholesale when non-empty. */}}
{{- define "tsagent.llmYaml" -}}
{{- if .Values.llm -}}
{{- toYaml .Values.llm -}}
{{- else -}}
{{- .Files.Get "files/config/llm.yaml" -}}
{{- end -}}
{{- end -}}

{{- define "tsagent.clustersYaml" -}}
{{- if .Values.clusters -}}
{{- toYaml .Values.clusters -}}
{{- else -}}
{{- .Files.Get "files/config/clusters.yaml" -}}
{{- end -}}
{{- end -}}

{{/* A probe from values, with the scheme switched to HTTPS when the pod serves TLS. */}}
{{- define "tsagent.probe" -}}
{{- $probe := deepCopy .probe -}}
{{- if and $probe.httpGet .root.Values.tls.serviceServingCert.enabled -}}
{{- $_ := set $probe.httpGet "scheme" "HTTPS" -}}
{{- end -}}
{{- toYaml $probe -}}
{{- end -}}
