# ledgerlens-fl-client

Standalone Python library for exchange partners to participate in LedgerLens federated learning without sharing raw trade data.

## Installation

```bash
pip install ledgerlens-fl-client
```

## Quick Start

```python
from ledgerlens_fl_client import FLClient, DataAdapter
import pandas as pd

class MyExchangeAdapter(DataAdapter):
    def trade_batches(self):
        # Yield batches of your private trade data
        df = pd.read_csv("my_trades.csv")
        yield df

client = FLClient(
    server_url="https://fl.ledgerlens.io",
    api_key="your-api-key",
    data_adapter=MyExchangeAdapter(),
    operator_id="exchange-xyz",
)

result = client.train_round()
print(f"Round {result.round_id}: accepted={result.accepted}")
```

## Features

- **Zero raw data sharing**: Only soft labels on a public synthetic dataset are transmitted
- **Differential privacy**: Configurable (ε, δ)-DP with Gaussian noise injection
- **Ed25519 authentication**: Cryptographically signed updates
- **Knowledge distillation**: FedAvg on soft labels compatible with RF/XGB/LGBM ensembles
- **Docker support**: Run as a container with environment variable configuration

## Documentation

See `docs/federation_integration.md` in the main repository for detailed integration guide.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a full history of releases and notable changes.

## License

MIT
## Docker

The client can run as a container for exchange-side federated learning participation, using environment variables for configuration instead of Python code.

### Build

```bash
docker build -t ledgerlens-fl-client packages/ledgerlens-fl-client/
docker run \
  -e FL_SERVER_URL=https://fl.ledgerlens.io \
  -e FL_API_KEY=your-api-key \
  -e FL_DATA_DIR=/data \
  -e FL_OPERATOR_ID=exchange-xyz \
  -e FL_ROUNDS=1 \
  -v /path/to/local/data:/data \
  ledgerlens-fl-client
**Now commit everything and push:**

```bash
git add contracts/oracle_aggregator/CHANGELOG.md contracts/oracle_aggregator/README.md contracts/zk_verifier/CHANGELOG.md contracts/zk_verifier/README.md packages/ledgerlens-fl-client/README.md

git commit -m "docs: document Docker build steps, panic messages, and add CHANGELOGs (#791, #792, #793, #794)

- Document Docker build/run/publish status for ledgerlens-fl-client
- Document all 4 panic! messages in oracle_aggregator's README table
- Add CHANGELOG.md to oracle_aggregator and zk_verifier contracts,
  reconstructed from git log, linked from each README"

git push origin docs/docker-panic-changelogs-batch
