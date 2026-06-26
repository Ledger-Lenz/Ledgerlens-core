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

## License

MIT