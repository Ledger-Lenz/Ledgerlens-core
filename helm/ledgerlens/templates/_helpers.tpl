{{- define "ledgerlens.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ledgerlens.labels" -}}
app.kubernetes.io/name: ledgerlens
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ledgerlens.selectorLabels" -}}
app.kubernetes.io/name: ledgerlens
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
