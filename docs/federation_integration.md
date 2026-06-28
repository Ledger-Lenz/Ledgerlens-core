# Federated Learning Integration Guide

This guide walks exchange partners through integrating with the LedgerLens federated learning network using the `ledgerlens-fl-client` library.

## Overview

LedgerLens federated learning enables multiple Stellar exchanges to collaboratively improve wash-trading detection models **without sharing raw trade data**. Each exchange:

1. Trains local models on their private data
2. Computes soft labels on a shared public synthetic dataset
3. Adds differential privacy noise
4. Submits signed gradient updates to the aggregation server
5. Receives aggregated global model for fine-tuning

**No raw transactions or model weights leave your infrastructure.**

---

## Prerequisites

- Python 3.10 or higher
- pip package manager
- Private labelled trade dataset (CSV format)
- Network access to the FL aggregation server

---

## Installation

```bash
pip install ledgerlens-fl-client
```

Verify installation:

```bash
python -c "from ledgerlens_fl_client import FLClient; print('Installation successful')"
```

---

## Quick Start

```python
from ledgerlens_fl_client import FLClient, DataAdapter
import pandas as pd

# Step 1: Implement a data adapter for your trade data
class MyExchangeAdapter(DataAdapter):
    def trade_batches(self):
        # Load your private labelled trade data
        df = pd.read_csv("my_exchange_trades.csv")
        yield df

# Step 2: Create the FL client
client = FLClient(
    server_url="https://fl.ledgerlens.io",
    api_key="your-api-key-here",
    data_adapter=MyExchangeAdapter(),
    operator_id="exchange-xyz",  # Your unique identifier
)

# Step 3: Participate in a federated round
result = client.train_round()

print(f"Round {result.round_id}:")
print(f"  Accepted: {result.accepted}")
print(f"  Samples: {result.n_samples}")
print(f"  Local AUC: {result.local_auc:.4f}")
```

---

## Implementing a DataAdapter

The `DataAdapter` abstract base class is how you provide your private trade data to the FL client.

### Required Interface

```python
from ledgerlens_fl_client import DataAdapter
from typing import Iterator
import pandas as pd

class MyExchangeAdapter(DataAdapter):
    def trade_batches(self) -> Iterator[pd.DataFrame]:
        """Yield batches of trade data as DataFrames.
        
        Each DataFrame must have:
        - Feature columns (float64): ML features for wash-trading detection
        - A 'label' column (int): 0 for normal, 1 for wash-trading
        """
        # Example: load from database
        import sqlalchemy
        engine = sqlalchemy.create_engine("postgresql://...")
        
        # Yield in batches to manage memory
        for batch_df in pd.read_sql("SELECT * FROM labelled_trades", engine, chunksize=10000):
            yield batch_df
```

### DataFrame Schema Requirements

Each yielded DataFrame must contain:

| Column Type | Name | Type | Description |
|-------------|------|------|-------------|
| Features | Any (auto-detected) | float64 | ML features (e.g., benford_chi_square, volume_concentration, etc.) |
| Label | `label` | int (0 or 1) | Ground truth: 0=normal, 1=wash-trading |

**Important:** Feature columns must match the schema expected by the LedgerLens model. See `detection/feature_engineering.py` in the main repository for the complete feature list.

### Using CSVDirectoryAdapter (Quick Start)

For CSV-based data, use the built-in adapter:

```python
from ledgerlens_fl_client import CSVDirectoryAdapter, FLClient

adapter = CSVDirectoryAdapter(
    directory="/path/to/trade_csvs",
    # Optional: specify feature columns explicitly
    # feature_columns=["feature_1", "feature_2", ..., "label"]
)

client = FLClient(
    server_url="https://fl.ledgerlens.io",
    api_key="your-key",
    data_adapter=adapter,
)
```

All CSV files in the directory will be read and concatenated.

---

## FLClient Constructor Reference

```python
client = FLClient(
    server_url: str,                    # Required: FL server URL
    api_key: str,                        # Required: Authentication key
    data_adapter: DataAdapter,           # Required: Your data adapter
    operator_id: str | None = None,      # Auto-generated UUID if None
    dp_epsilon: float = 1.0,             # Differential privacy epsilon
    dp_delta: float = 1e-5,              # Differential privacy delta
    gradient_clip_threshold: float = 10.0,  # L2 norm clip threshold
    noise_multiplier: float = 0.0,       # RDP noise multiplier (0 = legacy mode)
    ensemble_weight_rf: float = 0.25,    # Random forest weight
    ensemble_weight_xgb: float = 0.50,   # XGBoost weight
    ensemble_weight_lgbm: float = 0.25,  # LightGBM weight
    http_timeout: float = 60.0,          # HTTP request timeout (seconds)
)
```

### Parameter Details

| Parameter | Default | Description |
|-----------|---------|-------------|
| `server_url` | *(required)* | URL of the federated aggregation server |
| `api_key` | *(required)* | API key for server authentication |
| `data_adapter` | *(required)* | Instance of DataAdapter subclass |
| `operator_id` | Auto-generated | Unique identifier for your exchange |
| `dp_epsilon` | 1.0 | Privacy budget per round (lower = more privacy) |
| `dp_delta` | 1e-5 | Privacy failure probability |
| `gradient_clip_threshold` | 10.0 | Maximum L2 norm of gradient updates |
| `noise_multiplier` | 0.0 | RDP noise scale (set >0 for RDP accounting) |
| `ensemble_weight_*` | 0.25/0.50/0.25 | Weights for RF/XGB/LGBM models |
| `http_timeout` | 60.0 | HTTP request timeout in seconds |

---

## train_round() Return Values

The `train_round()` method returns a `RoundResult` dataclass:

```python
@dataclass
class RoundResult:
    round_id: str              # Unique round identifier (UUID)
    accepted: bool             # Whether server accepted your update
    reason: str                # Server response reason
    local_auc: float | None    # Local model AUC-ROC (if computable)
    n_samples: int             # Number of samples used this round
    n_valid_pending: int       # Valid updates pending at server
    quorum: int                # Minimum participants for aggregation
```

### Example Usage

```python
result = client.train_round()

if result.accepted:
    print(f"✓ Round {result.round_id} accepted")
    print(f"  Local AUC: {result.local_auc:.4f}")
    print(f"  Samples: {result.n_samples}")
    print(f"  Pending: {result.n_valid_pending}/{result.quorum}")
else:
    print(f"✗ Round {result.round_id} rejected: {result.reason}")
```

### Possible `reason` Values

| Reason | Meaning |
|--------|---------|
| `"ok"` | Update accepted normally |
| `"cosine_sim=X.XX < threshold"` | Update excluded due to low cosine similarity (gradient poisoning detection) |
| *(other)* | Server-specific rejection reasons |

---

## Round Scheduling

### Cron (Linux/macOS)

Run federated rounds hourly:

```bash
# /etc/cron.d/ledgerlens-fl
0 * * * * root /usr/bin/python -m ledgerlens_fl_client >> /var/log/fl-client.log 2>&1
```

### systemd Timer (Linux)

Create `/etc/systemd/system/ledgerlens-fl.service`:

```ini
[Unit]
Description=LedgerLens FL Client
After=network.target

[Service]
Type=oneshot
Environment="FL_SERVER_URL=https://fl.ledgerlens.io"
Environment="FL_API_KEY=your-key"
Environment="FL_DATA_DIR=/data/trades"
Environment="FL_OPERATOR_ID=exchange-xyz"
Environment="FL_ROUNDS=1"
ExecStart=/usr/bin/python -m ledgerlens_fl_client
```

Create `/etc/systemd/system/ledgerlens-fl.timer`:

```ini
[Unit]
Description=Run LedgerLens FL Client hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ledgerlens-fl.timer
sudo systemctl start ledgerlens-fl.timer
```

### Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: ledgerlens-fl-client
spec:
  schedule: "0 * * * *"  # Every hour
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: fl-client
            image: ledgerlens/fl-client:latest
            env:
            - name: FL_SERVER_URL
              value: "https://fl.ledgerlens.io"
            - name: FL_API_KEY
              valueFrom:
                secretKeyRef:
                  name: fl-secrets
                  key: api-key
            - name: FL_DATA_DIR
              value: "/data"
            - name: FL_OPERATOR_ID
              value: "exchange-xyz"
            - name: FL_ROUNDS
              value: "1"
            volumeMounts:
            - name: trade-data
              mountPath: /data
          volumes:
          - name: trade-data
            persistentVolumeClaim:
              claimName: trade-data-pvc
          restartPolicy: OnFailure
```

---

## Docker Deployment

### Build Image

```bash
cd packages/ledgerlens-fl-client
docker build -t ledgerlens/fl-client:latest .
```

### Run Container

```bash
docker run --rm \
  -e FL_SERVER_URL=https://fl.ledgerlens.io \
  -e FL_API_KEY=your-secret-key \
  -e FL_DATA_DIR=/data \
  -e FL_OPERATOR_ID=exchange-xyz \
  -e FL_ROUNDS=1 \
  -v /path/to/your/trade/data:/data:ro \
  ledgerlens/fl-client:latest
```

### Docker Compose

```yaml
version: '3.8'

services:
  fl-client:
    image: ledgerlens/fl-client:latest
    environment:
      FL_SERVER_URL: https://fl.ledgerlens.io
      FL_API_KEY: ${FL_API_KEY}
      FL_DATA_DIR: /data
      FL_OPERATOR_ID: exchange-xyz
      FL_ROUNDS: "1"
    volumes:
      - ./trade_data:/data:ro
    restart: "unless-stopped"
```

---

## Privacy Parameters Explained

### Differential Privacy (ε, δ)

- **`dp_epsilon` (ε)**: Privacy budget per round. Lower values = more privacy but lower model utility. Typical range: 0.1 to 2.0.
- **`dp_delta` (δ)**: Probability of privacy failure. Set to 1e-5 or lower.

**Trade-off**: Smaller ε → more noise → less accurate gradients → slower convergence.

### Gradient Clipping

- **`gradient_clip_threshold`**: Maximum L2 norm allowed for gradient updates. Prevents any single participant from dominating aggregation.

If your update norm exceeds this threshold, it's clipped proportionally.

### Noise Multiplier (RDP Path)

- **`noise_multiplier`**: When > 0, enables Rényi Differential Privacy (RDP) accounting with σ = clip_threshold × noise_multiplier.

RDP provides tighter privacy bounds than basic (ε, δ) composition over multiple rounds.

### Privacy Budget Exhaustion

The server tracks cumulative ε across rounds. When `cumulative_ε >= FEDERATED_DP_MAX_EPSILON` (default: 10.0), the server rejects new updates until an operator acknowledges the budget exhaustion (via reconfiguration).

---

## Troubleshooting

### "Connection refused" error

**Cause**: Cannot reach the FL server.

**Solution**:
- Verify `server_url` is correct and accessible
- Check firewall rules allow outbound HTTPS
- Test connectivity: `curl -I https://fl.ledgerlens.io`

### "Invalid signature" error

**Cause**: Client keypair generation issue.

**Solution**:
- This should not occur; the client auto-generates a valid Ed25519 keypair
- If persistent, delete any cached keys and restart

### "Privacy budget exhausted" error

**Cause**: Server has reached maximum cumulative ε.

**Solution**:
- Contact the FL network operator to reset or increase `FEDERATED_DP_MAX_EPSILON`
- Or wait for the next federation epoch

### "cosine_sim < threshold" rejection

**Cause**: Your gradient update direction differs significantly from other participants (possible gradient poisoning detection).

**Solution**:
- Verify your data labelling is correct
- Check that feature columns match the expected schema
- If legitimate, contact the server operator to review the exclusion

### "No module named 'numpy'" or import errors

**Cause**: Package dependencies not installed.

**Solution**:
```bash
pip install --upgrade pip
pip install ledgerlens-fl-client
```

Or in a clean virtualenv:
```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
.venv\Scripts\activate     # Windows
pip install ledgerlens-fl-client
```

### AUC-ROC is NaN or very low

**Cause**: All samples have the same label (no class diversity).

**Solution**:
- Ensure your dataset contains both normal (0) and wash-trading (1) labels
- Increase batch size to include more diverse samples

---

## Next Steps

1. **Generate your API key**: Contact the LedgerLens federation operator
2. **Prepare your labelled dataset**: Export trades with ground-truth compliance labels
3. **Test locally**: Run a single round against a local test server
4. **Deploy to production**: Schedule regular rounds via cron/systemd/Kubernetes

For questions or issues, open a GitHub issue or contact the LedgerLens team.