"""LedgerLens FL Client - Standalone federated learning library."""

from .adapter import DataAdapter, CSVDirectoryAdapter
from .client import FLClient
from .models import RoundResult, ClientStatus

__version__ = "0.1.0"

__all__ = [
    "FLClient",
    "DataAdapter",
    "CSVDirectoryAdapter",
    "RoundResult",
    "ClientStatus",
    "__version__",
]