---
title: "Add Kubernetes Deployment Manifests and Helm Chart for Cloud-Native Deployment"
labels: ["difficulty: advanced", "area: devops", "type: feature"]
assignees: []
---

## Summary
LedgerLens has no Kubernetes deployment artifacts, limiting production deployment options to single-host Docker Compose. A Helm chart covering the API server, ingestion workers, and feature store enables repeatable, configurable cloud-native deployment on any Kubernetes cluster.

## Objectives
- [ ] Create `helm/ledgerlens/` chart with templates for: API Deployment, Ingestion Worker Deployment, HPA, Service, Ingress, ConfigMap, Secret, PersistentVolumeClaim
- [ ] Values file with sensible defaults: `replicaCount: 2`, resource limits, ingress disabled by default
- [ ] `helm install ledgerlens ./helm/ledgerlens --set ingress.enabled=true` produces a working deployment
- [ ] Add liveness probe (`GET /health`) and readiness probe (`GET /health/ready`) to the API deployment
- [ ] Document in `docs/kubernetes_deployment.md`

## Definition of Done
- [ ] `helm lint` passes with no errors or warnings
- [ ] End-to-end test: deploy to local `kind` cluster, run smoke test against the API
- [ ] HPA scales API pods under load (target CPU 70%)
- [ ] Chart versioned in sync with app version via `Chart.appVersion`
