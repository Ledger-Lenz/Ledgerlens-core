"""Command-line interface for FL client (Docker entry point).

Usage
-----
    python -m ledgerlens_fl_client \
        --server-url https://fl.ledgerlens.io \
        --api-key your-key \
        --data-dir /path/to/data \
        --operator-id exchange-xyz \
        --rounds 5

Environment Variables
---------------------
    FL_SERVER_URL     : Server URL (required)
    FL_API_KEY        : API key (required)
    FL_DATA_DIR       : Directory containing CSV files (required)
    FL_OPERATOR_ID    : Operator identifier (optional, auto-generated)
    FL_ROUNDS         : Number of rounds to run (default: 1)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .adapter import CSVDirectoryAdapter
from .client import FLClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="LedgerLens Federated Learning Client",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--server-url",
        type=str,
        default=os.getenv("FL_SERVER_URL", ""),
        help="Federated aggregation server URL",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.getenv("FL_API_KEY", ""),
        help="API key for server authentication",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=os.getenv("FL_DATA_DIR", ""),
        help="Directory containing CSV trade data files",
    )
    parser.add_argument(
        "--operator-id",
        type=str,
        default=os.getenv("FL_OPERATOR_ID", None),
        help="Unique operator identifier",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=int(os.getenv("FL_ROUNDS", "1")),
        help="Number of federated rounds to participate in",
    )
    parser.add_argument(
        "--dp-epsilon",
        type=float,
        default=1.0,
        help="Differential privacy epsilon",
    )
    parser.add_argument(
        "--dp-delta",
        type=float,
        default=1e-5,
        help="Differential privacy delta",
    )
    parser.add_argument(
        "--gradient-clip-threshold",
        type=float,
        default=10.0,
        help="Gradient L2 norm clip threshold",
    )
    parser.add_argument(
        "--noise-multiplier",
        type=float,
        default=0.0,
        help="Noise multiplier for RDP path",
    )
    
    args = parser.parse_args()
    
    if not args.server_url:
        logger.error("FL_SERVER_URL environment variable or --server-url is required")
        return 1
    
    if not args.api_key:
        logger.error("FL_API_KEY environment variable or --api-key is required")
        return 1
    
    if not args.data_dir:
        logger.error("FL_DATA_DIR environment variable or --data-dir is required")
        return 1
    
    adapter = CSVDirectoryAdapter(directory=args.data_dir)
    
    try:
        client = FLClient(
            server_url=args.server_url,
            api_key=args.api_key,
            data_adapter=adapter,
            operator_id=args.operator_id,
            dp_epsilon=args.dp_epsilon,
            dp_delta=args.dp_delta,
            gradient_clip_threshold=args.gradient_clip_threshold,
            noise_multiplier=args.noise_multiplier,
        )
        
        for round_num in range(args.rounds):
            logger.info("Starting round %d/%d", round_num + 1, args.rounds)
            result = client.train_round()
            
            logger.info(
                "Round %s complete: accepted=%s, local_auc=%.4f, samples=%d",
                result.round_id,
                result.accepted,
                result.local_auc or 0.0,
                result.n_samples,
            )
        
        status = client.status()
        logger.info(
            "Federated participation complete: %d rounds, operator=%s",
            status.rounds_completed,
            status.operator_id,
        )
        
        return 0
    
    except Exception as exc:
        logger.error("FL client failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())