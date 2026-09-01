# LedgerLens Federated Learning Server

This is the standalone federated learning coordinator for LedgerLens.
It was extracted from `ledgerlens-core` to enable independent deployment.

## Installation

```bash
pip install -e .
```

## Running the Server

```bash
python -m ledgerlens_fl_server
```

Configuration is handled via environment variables. See `config.py` for supported settings.
