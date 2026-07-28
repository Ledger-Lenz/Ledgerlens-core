from detection.sar_narrative import generate_sar_narrative


def test_generate_sar_narrative_handles_missing_alerts():
    narrative = generate_sar_narrative(
        wallet="GABC",
        start_date="2026-07-01",
        end_date="2026-07-02",
        peak_score=72.4,
        alerts=None,
        volume_xlm=1000,
        n_pairs=2,
        cluster_size=3,
        chi_sq=1.234,
        chi_p=0.05678,
    )

    assert "HIGH risk" in narrative
    assert "No discrete manipulation alerts" in narrative
    assert "{" not in narrative


def test_generate_sar_narrative_skips_malformed_numeric_alert_detail():
    narrative = generate_sar_narrative(
        wallet="GABC",
        start_date="2026-07-01",
        end_date="2026-07-02",
        peak_score=91,
        alerts=[
            {
                "alert_type": "wash_cycle",
                "detail": {"cycle_volume": "not-a-number"},
                "asset_pair": "XLM/USDC",
                "timestamp": "2026-07-01T00:00:00Z",
            }
        ],
        volume_xlm=1000,
        n_pairs=2,
        cluster_size=3,
        chi_sq=1.234,
        chi_p=0.05678,
    )

    assert "Wash Cycle on XLM/USDC observed at 2026-07-01T00:00:00Z." in narrative
