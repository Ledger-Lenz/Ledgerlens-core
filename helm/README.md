# LedgerLens Helm Chart

Helm chart for deploying LedgerLens — Benford's Law + ensemble ML
wash-trading detection engine for the Stellar DEX.

## Prerequisites

- A Kubernetes cluster and `kubectl`/`helm` (v3+) configured against it.
- Required secrets, provided via `--set` or a values override file and
  rendered into a `Secret` by `templates/secret.yaml`:
  - `LEDGERLENS_SERVICE_SECRET_KEY`
  - `LEDGERLENS_MODEL_SIGNING_KEY`
  - `LEDGERLENS_ADMIN_API_KEY`
  - `LEDGERLENS_COMPLIANCE_API_KEY`
  - `LEDGERLENS_WEBHOOK_ENCRYPTION_KEY`
- If `ingress.enabled=true`, an ingress controller must already be installed
  in the cluster (the chart does not install one).
- If `autoscaling.enabled=true` (default), the `metrics-server` add-on must
  be available for the HorizontalPodAutoscaler to read CPU/memory metrics.

## Install

```bash
helm install ledgerlens ./helm/ledgerlens \
  --set secrets.LEDGERLENS_SERVICE_SECRET_KEY="<service-secret>" \
  --set secrets.LEDGERLENS_MODEL_SIGNING_KEY="<model-signing-key>" \
  --set secrets.LEDGERLENS_ADMIN_API_KEY="<admin-api-key>" \
  --set secrets.LEDGERLENS_COMPLIANCE_API_KEY="<compliance-api-key>" \
  --set secrets.LEDGERLENS_WEBHOOK_ENCRYPTION_KEY="<webhook-encryption-key>"
```

Or with a values override file (recommended for anything beyond a quick
test, so secrets aren't left in shell history):

```bash
helm install ledgerlens ./helm/ledgerlens -f values-override.yaml
```

See `ledgerlens/values.yaml` for the full set of configurable values
(resources, autoscaling, ingress, cost/capacity config, etc.), each
documented inline.

## Upgrading

```bash
helm upgrade ledgerlens ./helm/ledgerlens -f values-override.yaml
```

## Verifying the chart

```bash
helm lint ./helm/ledgerlens
helm template ledgerlens ./helm/ledgerlens
```
