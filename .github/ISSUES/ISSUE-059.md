---
title: "Build Federated Learning Client Library for Exchange-Side Participation Without Raw Data Sharing"
labels: ["difficulty: advanced", "area: federated-learning", "type: feature"]
assignees: []
---

## Summary
Exchange partners wishing to contribute to the LedgerLens federated learning network currently must run the full `federated/fl_client.py` module with manual configuration. This issue packages the federated client as a standalone Python library (`ledgerlens-fl-client`) with a clean API, Docker image, and documented integration guide, lowering the barrier for exchange partners to join the federation without exposing their raw trade data.

## Background & Context
The federated learning architecture (see `docs/federated_learning.md`) allows multiple Stellar exchanges to collaboratively improve the wash-trading detection models without sharing raw trade data. Instead, each exchange trains a local model on their own data and shares only gradient updates with the aggregation server.

Currently, the client is tightly coupled to the LedgerLens monorepo internals, making it impractical for external exchanges to integrate. A standalone library with a stable, versioned API removes this friction.

## Objectives
- [ ] Extract `federated/fl_client.py` into a standalone `packages/ledgerlens-fl-client/` Python package
- [ ] Define a clean public API: `FLClient(server_url, api_key, data_adapter)` with `train_round()` and `status()` methods
- [ ] Implement `DataAdapter` abstract base class that exchanges subclass to provide their own trade data iterator
- [ ] Publish Docker image `ledgerlens/fl-client:latest` with configurable server URL and API key
- [ ] Write integration guide `docs/federation_integration.md` covering setup, data adapter implementation, and round scheduling

## Technical Requirements
```python
from ledgerlens_fl_client import FLClient, DataAdapter

class MyExchangeAdapter(DataAdapter):
    def trade_batches(self) -> Iterator[pd.DataFrame]:
        # yield batches of local trade data
        ...

client = FLClient(
    server_url="https://fl.ledgerlens.io",
    api_key="...",
    data_adapter=MyExchangeAdapter(),
)
result = client.train_round()  # trains locally, submits gradients, returns round metrics
```

The library must have zero hard dependencies on the LedgerLens core monorepo.

## Definition of Done
- [ ] `pip install ledgerlens-fl-client` installs successfully in a clean virtualenv
- [ ] Example exchange integration runs end-to-end against a local test aggregation server
- [ ] `docs/federation_integration.md` published with step-by-step setup guide
- [ ] Docker image builds and passes smoke test

## For Contributors
Python packaging experience (pyproject.toml, publishing to PyPI) and ML training loop familiarity required.
