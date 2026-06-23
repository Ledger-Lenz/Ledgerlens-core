import pytest
from detection.oracle_node import OracleNode

class TestOracleConsensus:
    def test_mock_multi_validator_consensus(self):
        """
        Test: two validators submit score=75, one submits score=40
        Expected: finalized score = 75 (median)
        """
        # TODO: Setup 3 validators
        # TODO: Submit scores
        # TODO: Finalize round
        # TODO: Assert median = 75
        pass

    def test_finalize_requires_quorum(self):
        """Test: finalize_round reverts if fewer than MIN_VALIDATORS submissions before deadline"""
        # TODO: Setup oracle round
        # TODO: Submit only 2 scores (MIN_VALIDATORS = 3)
        # TODO: Assert finalize_round reverts
        pass

    def test_validator_outlier_alert(self):
        """Test: VALIDATOR_OUTLIER alert when submission deviates > 20 points from consensus"""
        # TODO: Setup 3 validators
        # TODO: Submit score=75, score=75, score=40
        # TODO: Assert outlier alert created in dispute_store
        pass
