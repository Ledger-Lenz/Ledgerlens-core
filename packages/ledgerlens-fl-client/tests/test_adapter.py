"""Tests for DataAdapter ABC and CSVDirectoryAdapter."""

import pytest
import pandas as pd

from ledgerlens_fl_client.adapter import DataAdapter, CSVDirectoryAdapter


def test_data_adapter_is_abstract():
    """DataAdapter cannot be instantiated without implementing trade_batches."""
    with pytest.raises(TypeError):
        DataAdapter()


def test_data_adapter_subclass_must_implement_trade_batches():
    """Subclass without trade_batches raises TypeError."""
    class IncompleteAdapter(DataAdapter):
        pass
    
    with pytest.raises(TypeError):
        IncompleteAdapter()


def test_data_adapter_subclass_works():
    """Valid subclass with trade_batches works."""
    class ValidAdapter(DataAdapter):
        def trade_batches(self):
            yield pd.DataFrame({"feature_1": [1.0, 2.0], "label": [0, 1]})
    
    adapter = ValidAdapter()
    batches = list(adapter.trade_batches())
    assert len(batches) == 1
    assert "label" in batches[0].columns


def test_csv_directory_adapter_reads_files(temp_csv_dir):
    """CSVDirectoryAdapter reads all CSV files from directory."""
    adapter = CSVDirectoryAdapter(directory=str(temp_csv_dir))
    batches = list(adapter.trade_batches())
    
    assert len(batches) == 3
    for batch in batches:
        assert "label" in batch.columns
        assert len(batch) == 50


def test_csv_directory_adapter_empty_dir(tmp_path):
    """CSVDirectoryAdapter yields nothing from empty directory."""
    adapter = CSVDirectoryAdapter(directory=str(tmp_path))
    batches = list(adapter.trade_batches())
    assert len(batches) == 0


def test_csv_directory_adapter_sorted_order(tmp_path):
    """CSV files are yielded in sorted order."""
    for name in ["z.csv", "a.csv", "m.csv"]:
        df = pd.DataFrame({"x": [1], "label": [0]})
        df.to_csv(tmp_path / name)
    
    adapter = CSVDirectoryAdapter(directory=str(tmp_path))
    batches = list(adapter.trade_batches())
    
    assert len(batches) == 3