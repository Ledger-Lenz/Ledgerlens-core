# Grafana Provisioning

This directory holds Grafana **provisioning** config — the YAML that tells a
Grafana instance to load dashboards (and, if added later, datasources) from the
filesystem automatically on startup, instead of requiring a manual **Dashboards
→ Import** through the UI.

See the upstream reference:
<https://grafana.com/docs/grafana/latest/administration/provisioning/#dashboards>

## What's in here

```
monitoring/grafana/provisioning/
└── dashboards/
    └── ledgerlens.yaml     # dashboard provider definition
```

### `dashboards/ledgerlens.yaml`

A single **dashboard provider**. The important fields:

| Field | Value | Meaning |
| ----- | ----- | ------- |
| `providers[].name` | `LedgerLens` | Provider id (must be unique within the Grafana instance). |
| `folder` | `LedgerLens` | Dashboards load into this Grafana folder. |
| `type` | `file` | Read dashboards from a directory on disk. |
| `options.path` | `/var/lib/grafana/dashboards/ledgerlens` | Directory Grafana scans **inside the container / server**. Every `*.json` file here is loaded as a dashboard. |
| `options.foldersFromFilesStructure` | `false` | Sub-directories are *not* turned into Grafana folders; everything lands in `folder` above. |
| `updateIntervalSeconds` | `30` | Grafana re-scans the path every 30 s, so new/edited JSON files appear without a restart. |
| `allowUiUpdates` / `editable` | `true` | You can tweak a provisioned dashboard in the UI, but "Save" writes back a warning — the file on disk is the source of truth. |

> There is currently **no datasources provisioning file**. The committed
> dashboard references a `${DS_PROMETHEUS}` datasource variable, so the target
> Grafana must already have a Prometheus datasource (selected once via the
> dashboard's datasource dropdown, or added as
> `provisioning/datasources/*.yaml`).

## Adding a new dashboard so it auto-loads

1. **Export the dashboard JSON.** In Grafana: dashboard → *Share* → *Export* →
   *Save to file* (leave "Export for sharing externally" **off** so panel
   datasources stay as the `${DS_PROMETHEUS}` variable rather than being
   hard-templated with `__inputs`).

2. **Set a stable `uid` and `title`** at the top level of the JSON, e.g.:

   ```json
   {
     "uid": "ledgerlens-core-detection",
     "title": "LedgerLens Core Detection",
     "schemaVersion": 38,
     "panels": [ ... ]
   }
   ```

   The `uid` becomes the dashboard URL (`/d/<uid>/...`) and must be unique and
   unchanging across edits. Keep `schemaVersion` at whatever your Grafana
   version writes (38+ for Grafana 10/11).

3. **Drop the file next to the existing dashboards.** In this repo, dashboard
   JSON lives in `monitoring/grafana/`:

   ```
   monitoring/grafana/cost_capacity_dashboard.json
   monitoring/grafana/<your_new_dashboard>.json      # add here
   ```

   There is **no filename convention** enforced by Grafana — any `*.json` in the
   provisioned `path` is picked up — but use `snake_case` ending in
   `_dashboard.json` to match the existing file.

4. **Make sure the file reaches the provisioned path** on the Grafana instance
   (`options.path`, i.e. `/var/lib/grafana/dashboards/ledgerlens`). How depends
   on how Grafana is deployed — see the next section.

5. **Wait ≤30 s** (the `updateIntervalSeconds`) or restart Grafana. The
   dashboard shows up in the **LedgerLens** folder with no manual import.

## How the provisioning path is wired up per deployment

### Bare Grafana server

Per `monitoring/README.md` → *Import Grafana Dashboard* → *Option B*:

```bash
sudo mkdir -p /var/lib/grafana/dashboards/ledgerlens
sudo cp monitoring/grafana/*.json           /var/lib/grafana/dashboards/ledgerlens/
sudo cp monitoring/grafana/provisioning/dashboards/ledgerlens.yaml \
        /etc/grafana/provisioning/dashboards/
sudo systemctl restart grafana-server
```

- provider YAML → `/etc/grafana/provisioning/dashboards/`
- dashboard JSON → the `options.path` directory
  (`/var/lib/grafana/dashboards/ledgerlens`)

### Docker Compose

The repository's `docker-compose.yml` does **not** currently run Grafana (only
`api`, `jaeger`, `toxiproxy`, `redis`). If you add a Grafana service, mount both
trees so the paths in `ledgerlens.yaml` line up:

```yaml
grafana:
  image: grafana/grafana:11.1.0
  profiles: [dev]
  ports:
    - "3000:3000"
  volumes:
    # provider config → Grafana's provisioning dir
    - ./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro
    # dashboard JSON → the path referenced by options.path in ledgerlens.yaml
    - ./monitoring/grafana:/var/lib/grafana/dashboards/ledgerlens:ro
```

### Kubernetes / Helm

Ship the provider YAML and dashboard JSON as ConfigMaps mounted at
`/etc/grafana/provisioning/dashboards/` and `options.path` respectively (or use
the `grafana` community chart's `dashboardProviders` + `dashboards` values).

## Verifying a new dashboard locally

Quick standalone check without touching an existing Grafana:

```bash
docker run --rm -p 3000:3000 \
  -v "$PWD/monitoring/grafana/provisioning:/etc/grafana/provisioning:ro" \
  -v "$PWD/monitoring/grafana:/var/lib/grafana/dashboards/ledgerlens:ro" \
  grafana/grafana:11.1.0

# then, after ~15s:
curl -s -u admin:admin http://localhost:3000/api/search?query=LedgerLens | jq '.[].title'
```

The new dashboard title should appear in that list (and under the **LedgerLens**
folder at <http://localhost:3000>) with no manual import step. Grafana logs
`msg="starting to provision dashboards"` / `finished to provision dashboards`
and will log a parse error naming the file if the JSON is malformed.

## Related

- [`monitoring/README.md`](../../README.md) — full monitoring setup, manual vs.
  provisioned dashboard import, Prometheus rule loading.
- [`docker-compose.yml`](../../../docker-compose.yml) — local service stack
  (Grafana not included by default; see the snippet above).
- `monitoring/grafana/cost_capacity_dashboard.json` — the existing provisioned
  dashboard, useful as a template.
