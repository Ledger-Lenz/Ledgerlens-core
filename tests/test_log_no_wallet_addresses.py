import logging
from datetime import datetime, timedelta, timezone
from ingestion.graph_builder import TemporalGraphBuilder


class FakeTrade:
    def __init__(self, b, c, amt, t):
        self.base_account, self.counter_account = b, c
        self.base_amount, self.ledger_close_time = amt, t
        self.price, self.trade_type = 1.0, "orderbook"


def test_no_wallet_address_at_info_level(caplog):
    wallet = "GFULLSECRETADDRESS123456789"
    trades = [FakeTrade(wallet, "GOTHER", 10.0, datetime.now(timezone.utc) - timedelta(minutes=1))]
    with caplog.at_level(logging.INFO):
        TemporalGraphBuilder().build_snapshots(trades, lookback_days=1)
    for record in caplog.records:
        if record.levelno >= logging.INFO and record.levelno < logging.DEBUG + 1:
            assert wallet not in record.getMessage()
