# Helm Charts

## `ledgerlens/`

The main Helm chart for deploying LedgerLens.

## `chaos-mesh-values.yaml`

This is a **values file for the third-party [Chaos Mesh](https://chaos-mesh.org/) chart**, not part of the LedgerLens chart itself. It configures a minimal Chaos Mesh controller installation (single replica, RBAC/service account enabled, UI disabled) to run chaos experiments (see `chaos-mesh/`) against the LedgerLens deployment in the same cluster.

Install Chaos Mesh into the cluster with this values file:

```bash
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo update
helm install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh --create-namespace \
  -f helm/chaos-mesh-values.yaml
```

To upgrade an existing installation:

```bash
helm upgrade chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh \
  -f helm/chaos-mesh-values.yaml
```

Once installed, the chaos experiment manifests in `chaos-mesh/` (pod-kill, network-partition, etc.) can be applied against the cluster.
