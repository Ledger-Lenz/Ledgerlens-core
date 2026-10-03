# LedgerLens Helm Charts

This directory contains the Helm chart and values files used to deploy
LedgerLens on Kubernetes.

## Contents

- **`ledgerlens/`** — The official LedgerLens Helm chart.
  - `Chart.yaml` — Chart metadata (name, version, description)
  - `values.yaml` — Default configuration: replica count, image, service,
    ingress, API/worker probes and resources, autoscaling, ConfigMap settings,
    secrets, persistence, ServiceAccount, and cost/capacity configuration
  - `templates/` — Kubernetes manifests rendered by the chart (deployments,
    ConfigMap, Secret, Service, Ingress, HPA, PVC, ServiceAccount, cost config)
- **`chaos-mesh-values.yaml`** — Values file for deploying Chaos Mesh
  (`pingcap/chaos-mesh`) alongside the chart for chaos-engineering tests.

## Quick start

```bash
helm install ledgerlens ./helm/ledgerlens
```

Override defaults with `--set` (e.g. `--set ingress.enabled=true`) or a custom
values file:

```bash
helm install ledgerlens ./helm/ledgerlens -f my-values.yaml
```

## Supported Kubernetes versions

The chart supports **Kubernetes 1.28 – 1.31**. It relies on
`autoscaling/v2` (HPA, GA since 1.23), `networking.k8s.io/v1` (Ingress,
GA since 1.19) and, when `canary.enabled=true`, the Argo Rollouts
`argoproj.io/v1alpha1` CRDs.

CI (`.github/workflows/helm-lint.yml`) renders the chart with
`helm template --kube-version` and validates it with `kubeconform -strict`
against each supported version, for two values profiles:

- `default` — `ledgerlens/values.yaml`
- `production` — `ledgerlens/ci/production-values.yaml` (canary Rollout,
  Ingress with TLS, PVC, external PostgreSQL)

A resource using an API version that is removed or unavailable on any
supported version (e.g. `autoscaling/v2beta2` HPA) fails the job. When
changing the supported range, update both this section and the workflow
matrix.

## Further reading

- [docs/kubernetes_deployment.md](../docs/kubernetes_deployment.md) — Full deployment guide and parameter reference
- [docs/cost_and_capacity.md](../docs/cost_and_capacity.md) — Cost and capacity configuration
- [docs/observability.md](../docs/observability.md) — Metrics, logging, and alerting
