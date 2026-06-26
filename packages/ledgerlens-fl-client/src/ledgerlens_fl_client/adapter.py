from abc import ABC, abstractmethod
from typing import Iterator
from pathlib import Path

import pandas as pd


class DataAdapter(ABC):
    """Abstract base class for exchange trade data adapters.
    
    Exchange partners subclass this to provide their local trade data
    to the federated learning client. The adapter yields batches of
    trade data as pandas DataFrames.
    
    Each DataFrame must contain:
    - Feature columns (float64) matching the public dataset feature count
    - A 'label' column (int: 0 or 1) indicating wash-trading classification
    """
    
    @abstractmethod
    def trade_batches(self) -> Iterator[pd.DataFrame]:
        """Yield batches of local trade data as DataFrames.
        
        Returns
        -------
        Iterator[pd.DataFrame]
            Iterator over DataFrames, each containing feature columns
            and a 'label' column.
        """
        ...


class CSVDirectoryAdapter(DataAdapter):
    """Convenience adapter that reads CSV files from a directory.
    
    Each CSV file must have columns matching the expected feature schema
    plus a 'label' column (0 or 1).
    
    Example
    -------
        adapter = CSVDirectoryAdapter(
            directory="/path/to/trade_data",
            feature_columns=["feature_1", "feature_2", ..., "label"]
        )
        client = FLClient(..., data_adapter=adapter)
    """
    
    def __init__(self, directory: str, feature_columns: list[str] | None = None):
        """Initialize CSV directory adapter.
        
        Parameters
        ----------
        directory : str
            Path to directory containing CSV files.
        feature_columns : list[str] | None
            Optional list of feature column names. If None, all columns
            except 'label' are treated as features.
        """
        self.directory = directory
        self.feature_columns = feature_columns
    
    def trade_batches(self) -> Iterator[pd.DataFrame]:
        """Yield CSV files from the directory as DataFrames.
        
        Yields
        ------
        pd.DataFrame
            DataFrame loaded from each CSV file, sorted by filename.
        """
        dir_path = Path(self.directory)
        for csv_file in sorted(dir_path.glob("*.csv")):
            df = pd.read_csv(csv_file)
            yield df