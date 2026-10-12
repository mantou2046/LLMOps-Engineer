{{/*
Expand the name of the chart.
*/}}
{{- define "llmops-chart.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "llmops-chart.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "llmops-chart.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "llmops-chart.labels" -}}
helm.sh/chart: {{ include "llmops-chart.chart" . }}
{{ include "llmops-chart.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "llmops-chart.selectorLabels" -}}
app.kubernetes.io/name: {{ include "llmops-chart.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "llmops-chart.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "llmops-chart.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
ConfigMap 名字
- values.configMap.name 有值 → 用它（多环境共用一份外部 ConfigMap 时可指定）
- 否则 → <fullname>-config
⚠️ 不用裸 fullname：ConfigMap 与 Deployment 同名虽合法，但 kubectl get 时容易看混。
*/}}
{{- define "llmops-chart.configMapName" -}}
{{- default (printf "%s-config" (include "llmops-chart.fullname" .)) .Values.configMap.name }}
{{- end }}

{{/*
Secret 名字
- values.secret.name 有值 → 用它（如复用外部管理的 Secret）
- 否则 → <fullname>-secret
*/}}
{{- define "llmops-chart.secretName" -}}
{{- default (printf "%s-secret" (include "llmops-chart.fullname" .)) .Values.secret.name }}
{{- end }}
