{{- define "ledgerlens.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ledgerlens.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "ledgerlens.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ledgerlens.labels" -}}
helm.sh/chart: {{ include "ledgerlens.chart" . }}
{{ include "ledgerlens.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "ledgerlens.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ledgerlens.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "ledgerlens.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "ledgerlens.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Deployment-traceability annotations (Grand 5 / #702, Required scope C).

Populated by cd.yml's `helm upgrade --set-string deployment.meta.*` on every
deploy, so "what commit and image digest is actually running" is a question
answerable from the cluster itself — via `helm get metadata ledgerlens` or
`kubectl get deployment/rollout -o jsonpath='{.metadata.annotations}'` —
without needing to cross-reference CI logs that may have already rotated out
of retention.

All four values default to "unknown" (not omitted) when unset, so a `helm
template`/`helm install` run outside cd.yml (a local dry-run, a manual
`helm install` before this pipeline change existed on a given branch) still
produces a valid, inspectable annotation block instead of silently emitting
no traceability metadata at all — an empty annotation being indistinguishable
from "no one set it" is exactly the kind of silent gap this Grand exists to
close, so this template intentionally always writes something.
*/}}
{{- define "ledgerlens.deploymentMetaAnnotations" -}}
ledgerlens.io/commit-sha: {{ .Values.deployment.meta.commitSha | default "unknown" | quote }}
ledgerlens.io/image-digest: {{ .Values.deployment.meta.imageDigest | default "unknown" | quote }}
ledgerlens.io/deployed-at: {{ .Values.deployment.meta.deployedAt | default "unknown" | quote }}
ledgerlens.io/workflow-run-id: {{ .Values.deployment.meta.workflowRunId | default "unknown" | quote }}
{{- end }}
