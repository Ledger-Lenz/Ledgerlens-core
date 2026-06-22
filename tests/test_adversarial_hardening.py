from ingestion.synthetic_data import generate_synthetic_dataset
from detection.dataset import build_training_dataset
from detection.model_training import train_ensemble


def test_adversarial_hardening_runs():
    trades, meta, events, labels = generate_synthetic_dataset(n_normal_accounts=30, n_wash_rings=5, ring_size=3, seed=1)
    df = build_training_dataset(trades, labels, account_metadata=meta, order_book_events=events)
    # run hardening (should not raise)
    results = train_ensemble(df, adversarial_augment=False, adversarial_hardening=True)
    assert isinstance(results, dict)
    assert all(k in results for k in ("random_forest", "xgboost", "lightgbm"))
