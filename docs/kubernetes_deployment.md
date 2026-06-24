# Kubernetes Deployment

LedgerLens ships a Helm chart in `helm/ledgerlens/` for deploying on any
Kubernetes cluster.

## Prerequisites

- Kubernetes 1.24+
- Helm 3.x
- A container image built from the project `Dockerfile`

## Quick Start

```bash
# Build and push the container image
docker build -t ledgerlens/core:latest .

# Install with default values (ingress disabled)
helm install ledgerlens ./helm/ledgerlens

# Install with ingress enabled
helm install ledgerlens ./helm/ledgerlens --set ingress.enabled=true

# Install with custom values
helm install ledgerlens ./helm/ledgerlens -f my-values.yaml
```

## Components

| Component | Template | Description |
|-----------|----------|-------------|
| API Server | `api-deployment.yaml` | FastAPI app serving `/scores`, `/alerts`, `/health` |
| Ingestion Worker | `worker-deployment.yaml` | Horizon SSE streamer + scoring pipeline |
| Service | `service.yaml` | ClusterIP service for the API |
| Ingress | `ingress.yaml` | Optional ingress (disabled by default) |
| ConfigMap | `configmap.yaml` | Non-secret environment variables |
| Secret | `secret.yaml` | Sensitive environment variables (API keys) |
| PVC | `pvc.yaml` | Persistent storage for models and SQLite DB |
| HPA | `hpa.yaml` | Horizontal Pod Autoscaler (disabled by default) |

## Health Probes

The API deployment includes Kubernetes health probes:

- **Liveness**: `GET /health` — returns 200 when the process is alive and DB is reachable
- **Readiness**: `GET /health/ready` — returns 200 only when models are loaded and the API can serve scoring requests

## Configuration

### values.yaml Defaults

```yaml
api:
  replicaCount: 2
  resources:
    requests: { cpu: 250m, memory: 512Mi }
    limits:   { cpu: "1", memory: 1Gi }

ingestionWorker:
  replicaCount: 2

ingress:
  enabled: false  # set to true for external access

autoscaling:
  enabled: false
  minReplicas: 2
  maxReplicas: 10
  targetCPUUtilizationPercentage: 70
```

### Secrets

Pass sensitive values via `secretEnv`:

```bash
helm install ledgerlens ./helm/ledgerlens \
  --set secretEnv.LEDGERLENS_ADMIN_API_KEY=my-secret-key \
  --set secretEnv.LEDGERLENS_SERVICE_SECRET_KEY=my-stellar-key
```

### Autoscaling

```bash
helm install ledgerlens ./helm/ledgerlens \
  --set autoscaling.enabled=true \
  --set autoscaling.maxReplicas=20
```

## Upgrading

```bash
helm upgrade ledgerlens ./helm/ledgerlens -f my-values.yaml
```

## Uninstalling

```bash
helm uninstall ledgerlens
```
