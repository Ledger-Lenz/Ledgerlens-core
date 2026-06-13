from ingestion.synthetic_data import generate_synthetic_dataset


def test_generate_synthetic_dataset_shapes():
    trades, account_metadata, events, labels = generate_synthetic_dataset(
        n_normal_accounts=5, n_wash_rings=2, ring_size=3, trades_per_normal=4, trades_per_wash=6
    )

    assert not trades.empty
    assert set(labels.values()) == {0, 1}
    assert len(labels) == 5 + 2 * 3
    assert set(account_metadata.keys()) == set(labels.keys())

    expected_columns = {
        "id",
        "ledger_close_time",
        "base_account",
        "counter_account",
        "base_asset",
        "counter_asset",
        "base_amount",
        "counter_amount",
        "price",
        "base_is_seller",
    }
    assert expected_columns.issubset(trades.columns)


def test_wash_accounts_have_round_lot_amounts():
    trades, _, _, labels = generate_synthetic_dataset(
        n_normal_accounts=5, n_wash_rings=1, ring_size=3, trades_per_normal=4, trades_per_wash=10
    )
    wash_accounts = {a for a, label in labels.items() if label == 1}
    wash_trades = trades[
        trades["base_account"].isin(wash_accounts) & trades["counter_account"].isin(wash_accounts)
    ]
    assert not wash_trades.empty
    # Wash-ring amounts are drawn from a small fixed set of round lots.
    assert wash_trades["base_amount"].nunique() <= 6


def test_is_deterministic_with_seed():
    trades_a, _, _, labels_a = generate_synthetic_dataset(seed=7, n_normal_accounts=3, n_wash_rings=1, ring_size=2)
    trades_b, _, _, labels_b = generate_synthetic_dataset(seed=7, n_normal_accounts=3, n_wash_rings=1, ring_size=2)

    assert labels_a == labels_b
    assert trades_a["base_amount"].tolist() == trades_b["base_amount"].tolist()
