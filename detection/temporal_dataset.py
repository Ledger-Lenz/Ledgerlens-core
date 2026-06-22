"""Time series dataset builder for temporal risk score sequences.

Provides tools to build training and inference sequences from the SQLite database
and compute pairwise score correlations for wallet clusters.
"""

import json
import sqlite3
from datetime import timedelta
import numpy as np
import pandas as pd


def get_wallet_cluster(db_path: str, wallet: str) -> list[str]:
    """Find the latest wash ring (cluster) containing the wallet from database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT accounts_json FROM wash_rings ORDER BY detected_at DESC")
        rows = cursor.fetchall()
        for row in rows:
            accounts = json.loads(row[0])
            if wallet in accounts:
                return accounts
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return [wallet]


def cluster_score_correlation(wallets: list[str], scores_df: pd.DataFrame) -> float:
    """Returns mean pairwise Pearson correlation of 30d score time series.

    If wallets have fewer than 2 items, returns 0.0.
    """
    if len(wallets) < 2 or scores_df.empty:
        return 0.0

    # Filter for wallets of interest
    df = scores_df[scores_df["wallet"].isin(wallets)].copy()
    if df.empty:
        return 0.0

    # Ensure timestamp is datetime and floor to day to align them
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date

    # Group by date and wallet and take the mean score
    grouped = df.groupby(["date", "wallet"])["score"].mean().reset_index()

    # Pivot so each wallet is a column, indexed by date
    pivot_df = grouped.pivot(index="date", columns="wallet", values="score")

    # We need at least 2 columns to compute correlation
    if pivot_df.shape[1] < 2:
        return 0.0

    # Interpolate/fill missing values to align time series
    pivot_df = pivot_df.ffill().bfill().fillna(20.0)

    # Compute Pearson correlation matrix
    corr_matrix = pivot_df.corr(method="pearson")

    # Extract pairwise correlations (upper triangle excluding diagonal)
    corrs = []
    cols = list(corr_matrix.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            val = corr_matrix.iloc[i, j]
            if not pd.isna(val):
                corrs.append(val)

    if not corrs:
        return 0.0

    return float(np.mean(corrs))


def get_wallet_history(db_path: str, wallet: str) -> pd.DataFrame:
    """Fetch history of scores and features for a wallet from the DB."""
    conn = sqlite3.connect(db_path)
    query = """
        SELECT rs.score, fv.features_json, rs.timestamp
        FROM risk_scores rs
        LEFT JOIN feature_vectors fv ON rs.wallet = fv.wallet AND rs.asset_pair = fv.asset_pair
             AND ABS(strftime('%s', rs.timestamp) - strftime('%s', fv.timestamp)) < 10
        WHERE rs.wallet = ?
        ORDER BY rs.timestamp ASC
    """
    try:
        df = pd.read_sql_query(query, conn, params=(wallet,))
    except sqlite3.OperationalError:
        df = pd.DataFrame(columns=["score", "features_json", "timestamp"])
    finally:
        conn.close()
    return df


def get_daily_history(db_path: str, wallet: str) -> pd.DataFrame:
    """Load and aggregate history to daily frequency."""
    history = get_wallet_history(db_path, wallet)
    if history.empty:
        return pd.DataFrame()

    # Extract date
    history["date"] = pd.to_datetime(history["timestamp"]).dt.date

    # Parse features_json
    parsed_features = []
    for f_json in history["features_json"]:
        if f_json:
            try:
                parsed_features.append(json.loads(f_json))
            except Exception:
                parsed_features.append({})
        else:
            parsed_features.append({})

    # Convert list of dicts to DataFrame
    feats_df = pd.DataFrame(parsed_features)
    # Combine with score and date
    history = pd.concat([history[["date", "score"]], feats_df], axis=1)

    # Group by date and take mean of all numeric columns
    grouped = history.groupby("date").mean().reset_index()
    return grouped


def build_score_sequences(
    db_path: str,
    wallet: str,
    window_days: int = 30,
    stride_days: int = 1,
    sequence_length: int = 30,
) -> np.ndarray:
    """Returns array of shape (N_sequences, sequence_length, n_features).

    Features per timestep: [risk_score, benford_chi_sq, graph_density, volume_xlm, cluster_synchrony]
    """
    grouped = get_daily_history(db_path, wallet)
    cluster_wallets = get_wallet_cluster(db_path, wallet)

    # If history is empty, return empty array of shape (0, sequence_length, 5)
    if grouped.empty:
        return np.zeros((0, sequence_length, 5), dtype=np.float32)

    # Sort grouped history by date to ensure proper ordering
    grouped = grouped.sort_values("date").reset_index(drop=True)

    # We need to compute cluster synchrony correlation over time
    # Retrieve score histories of all cluster members
    conn = sqlite3.connect(db_path)
    if len(cluster_wallets) >= 3:
        placeholders = ",".join("?" for _ in cluster_wallets)
        query = f"""
            SELECT wallet, score, timestamp FROM risk_scores
            WHERE wallet IN ({placeholders})
        """
        try:
            scores_df = pd.read_sql_query(query, conn, params=cluster_wallets)
        except sqlite3.OperationalError:
            scores_df = pd.DataFrame(columns=["wallet", "score", "timestamp"])
    else:
        scores_df = pd.DataFrame(columns=["wallet", "score", "timestamp"])
    conn.close()

    # Pre-compute date-to-synchrony mapping
    dates = list(grouped["date"])
    synchrony_values = []

    # Pre-compute global pivot table for cluster
    w_pivot = None
    if len(cluster_wallets) >= 3 and not scores_df.empty:
        try:
            scores_df["date"] = pd.to_datetime(scores_df["timestamp"]).dt.date
            w_grouped = scores_df.groupby(["date", "wallet"])["score"].mean().reset_index()
            w_pivot = w_grouped.pivot(index="date", columns="wallet", values="score")
        except Exception:
            pass

    for t in range(len(dates)):
        # 30-day window ending at t
        window_end_date = dates[t]
        window_start_date = window_end_date - timedelta(days=window_days - 1)
        window_dates = [window_start_date + timedelta(days=d) for d in range(window_days)]
        
        # Calculate correlation for this window
        if w_pivot is None or w_pivot.empty:
            synchrony_values.append(0.0)
        else:
            window_pivot = w_pivot.reindex(window_dates)
            window_pivot = window_pivot.ffill().bfill().fillna(20.0)
            
            if window_pivot.shape[1] < 3:
                synchrony_values.append(0.0)
            else:
                corr_matrix = window_pivot.corr(method="pearson")
                corrs = []
                cols = list(corr_matrix.columns)
                for i in range(len(cols)):
                    for j in range(i + 1, len(cols)):
                        val = corr_matrix.iloc[i, j]
                        if not pd.isna(val):
                            corrs.append(val)
                mean_corr = float(np.mean(corrs)) if corrs else 0.0
                synchrony_values.append(1.0 if mean_corr > 0.85 else 0.0)

    # Build features matrix
    feature_matrix = np.zeros((len(grouped), 5), dtype=np.float32)
    feature_matrix[:, 0] = grouped["score"].values
    feature_matrix[:, 1] = grouped.get("benford_chi_square_30d", 0.0).fillna(0.0).values
    feature_matrix[:, 2] = grouped.get("network_centrality", 0.0).fillna(0.0).values
    feature_matrix[:, 3] = grouped.get("volume_to_unique_counterparty_ratio", 0.0).fillna(0.0).values
    feature_matrix[:, 4] = synchrony_values

    L = len(grouped)
    if L < sequence_length:
        # Pre-pad with zeros
        padded = np.zeros((sequence_length, 5), dtype=np.float32)
        padded[sequence_length - L :] = feature_matrix
        return np.expand_dims(padded, axis=0)

    sequences = []
    # Sliding window
    for start in range(0, L - sequence_length + 1, stride_days):
        end = start + sequence_length
        sequences.append(feature_matrix[start:end])

    return np.array(sequences, dtype=np.float32)


def generate_synthetic_sequence(label: int, sequence_length: int = 30) -> np.ndarray:
    """Generate a single synthetic sequence of features."""
    seq = np.zeros((sequence_length, 5), dtype=np.float32)
    if label == 1:
        # Wash trader pattern: slow-burn ramp-up or oscillation
        pattern_type = np.random.choice(["ramp_up", "oscillation"])
        if pattern_type == "ramp_up":
            scores = np.linspace(20.0, 70.0, sequence_length) + np.random.normal(0.0, 3.0, sequence_length)
        else:
            scores = 47.5 + 17.5 * np.sin(np.linspace(0, 4 * np.pi, sequence_length)) + np.random.normal(0.0, 2.0, sequence_length)
        
        scores = np.clip(scores, 0, 100)
        benford = np.linspace(2.0, 15.0, sequence_length) + np.random.normal(0.0, 0.5, sequence_length)
        density = np.linspace(0.05, 0.4, sequence_length) + np.random.normal(0.0, 0.02, sequence_length)
        volume = np.linspace(100.0, 3000.0, sequence_length) + np.random.exponential(100.0, sequence_length)
        synchrony = np.ones(sequence_length, dtype=np.float32) * float(np.random.choice([0.0, 1.0], p=[0.2, 0.8]))
    else:
        # Clean pattern: low scores with noise
        scores = np.random.uniform(15.0, 35.0, sequence_length) + np.random.normal(0.0, 2.0, sequence_length)
        scores = np.clip(scores, 0, 100)
        benford = np.random.uniform(0.5, 3.0, sequence_length)
        density = np.random.uniform(0.01, 0.08, sequence_length)
        volume = np.random.exponential(150.0, sequence_length)
        synchrony = np.zeros(sequence_length, dtype=np.float32)

    seq[:, 0] = scores
    seq[:, 1] = benford
    seq[:, 2] = density
    seq[:, 3] = volume
    seq[:, 4] = synchrony
    return seq


def build_training_sequences(
    df: pd.DataFrame,
    db_path: str,
    sequence_length: int = 30,
) -> tuple[np.ndarray, np.ndarray]:
    """Build score sequences for all training wallets.

    If database contains < 7 entries for a wallet, we fall back to generating a synthetic
    sequence of length 30 based on its label.
    """
    X_list = []
    y_list = []

    for _, row in df.iterrows():
        wallet = row["wallet"]
        label = int(row["label"])

        # Check database records count
        grouped = get_daily_history(db_path, wallet)
        if len(grouped) >= 7:
            # Build from DB
            seqs = build_score_sequences(db_path, wallet, sequence_length=sequence_length)
            for seq in seqs:
                X_list.append(seq)
                y_list.append(label)
        else:
            # Fallback: generate synthetic sequence
            X_list.append(generate_synthetic_sequence(label, sequence_length))
            y_list.append(label)

    return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=np.float32)
