"""Load bundled public dataset for federated learning.

The public dataset is a pre-generated numpy array derived from synthetic
trades with seed=0. All FL participants use the same public dataset to
ensure protocol compatibility.
"""

from pathlib import Path

import numpy as np


_DATA_FILE = Path(__file__).parent / "data" / "public_dataset_seed0.npz"


def get_public_dataset() -> np.ndarray:
    """Load the bundled public dataset.
    
    Returns
    -------
    np.ndarray
        Feature matrix X_pub (n_samples, n_features).
    
    Raises
    ------
    FileNotFoundError
        If the bundled dataset file is missing.
    """
    if not _DATA_FILE.exists():
        raise FileNotFoundError(
            f"Public dataset not found at {_DATA_FILE}. "
            "Ensure the package was installed correctly."
        )
    
    data = np.load(_DATA_FILE)
    return data["X_pub"]