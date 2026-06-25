# Kubernetes Deployment

LedgerLens ships with a Helm chart for repeatable, configurable deployment on any Kubernetes cluster.

## Prerequisites

- Kubernetes 1.24+
- Helm 3.8+
- A container registry with the `ledgerlens/core` image

## Quick Start

```bash
# Install the chart with default values
helm install ledgerlens ./helm/ledgerlens

# Install with ingress enabled
helm install ledgerlens ./helm/ledgerlens --set ingress.enabled=true

# Install with custom values file
helm install ledgerlens ./helm/ledgerlens -f my-values.yaml
```

## Configuration

The following table lists the configurable parameters and their defaults.

### Global

| Parameter | Default | Description |
|-----------|---------|-------------|
| `replicaCount` | `2` | Number of API server replicas |
| `image.repository` | `ledgerlens/core` | Container image repository |
| `image.tag` | `latest` | Container image tag |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy |

### API Server

| Parameter | Default | Description |
|-----------|---------|-------------|
| `api.resources.requests.cpu` | `500m` | CPU request per API replica |
| `api.resources.requests.memory` | `512Mi` | Memory request per API replica |
| `api.resources.limits.cpu` | `1000m` | CPU limit per API replica |
| `api.resources.limits.memory` | `1Gi` | Memory limit per API replica |
| `api.livenessProbe.httpGet.path` | `/health` | Liveness probe endpoint |
| `api.readinessProbe.httpGet.path` | `/health/ready` | Readiness probe endpoint |

### Ingestion Worker

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ingestionWorker.enabled` | `true` | Enable the ingestion worker deployment |
| `ingestionWorker.replicaCount` | `1` | Number of ingestion worker replicas |

### Autoscaling

| Parameter | Default | Description |
|-----------|---------|-------------|
| `autoscaling.enabled` | `true` | Enable HPA for the API deployment |
| `autoscaling.minReplicas` | `2` | Minimum API replicas |
| `autoscaling.maxReplicas` | `10` | Maximum API replicas |
| `autoscaling.targetCPUUtilizationPercentage` | `70` | Target CPU utilization |
| `autoscaling.targetMemoryUtilizationPercentage` | `80` | Target memory utilization |

### Ingress

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ingress.enabled` | `false` | Enable ingress (disabled by default) |
| `ingress.className` | `""` | Ingress class name |
| `ingress.annotations` | `{}` | Ingress annotations |
| `ingress.hosts` | `[{"host":"ledgerlens.local","paths":[{"path":"/","pathType":"Prefix"}]}]` | Ingress host rules |
| `ingress.tls` | `[]` | TLS configuration |

### Persistence

| Parameter | Default | Description |
|-----------|---------|-------------|
| `persistence.enabled` | `true` | Enable persistent volume |
| `persistence.storageClass` | `""` | Storage class (default cluster class if empty) |
| `persistence.accessMode` | `ReadWriteOnce` | PVC access mode |
| `persistence.size` | `10Gi` | PVC size |

## Probes

### Liveness Probe (API)

```
HTTP GET /health
Initial delay: 10s
Period: 15s
Timeout: 5s
Failure threshold: 3
```

### Readiness Probe (API)

```
HTTP GET /health/ready
Initial delay: 5s
Period: 10s
Timeout: 3s
Failure threshold: 2
```

## Deploying with Secrets

```bash
helm install ledgerlens ./helm/ledgerlens \
  --set ingress.enabled=true \
  --set secrets.LEDGERLENS_ADMIN_API_KEY=my-admin-key \
  --set secrets.LEDGERLENS_COMPLIANCE_API_KEY=my-compliance-key \
  --set secrets.LEDGERLENS_SERVICE_SECRET_KEY=my-soroban-secret \
  --set secrets.LEDGERLENS_MODEL_SIGNING_KEY=my-signing-key \
  --set secrets.LEDGERLENS_WEBHOOK_ENCRYPTION_KEY=my-webhook-key
```

## Uninstalling

```bash
helm uninstall ledgerlens
```

## Chart Structure

```
helm/ledgerlens/
├── Chart.yaml
├── values.yaml
├── .helmignore
└── templates/
    ├── _helpers.tpl
    ├── api-deployment.yaml
    ├── ingestion-worker-deployment.yaml
    ├── hpa.yaml
    ├── service.yaml
    ├── ingress.yaml
    ├── configmap.yaml
    ├── secret.yaml
    ├── pvc.yaml
    └── serviceaccount.yaml
```
