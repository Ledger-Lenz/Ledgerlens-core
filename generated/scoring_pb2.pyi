from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class ScoreRequest(_message.Message):
    __slots__ = ("wallet", "asset_pair")
    WALLET_FIELD_NUMBER: _ClassVar[int]
    ASSET_PAIR_FIELD_NUMBER: _ClassVar[int]
    wallet: str
    asset_pair: str
    def __init__(self, wallet: _Optional[str] = ..., asset_pair: _Optional[str] = ...) -> None: ...

class RiskScoreProto(_message.Message):
    __slots__ = ("wallet", "asset_pair", "score", "benford_flag", "ml_flag", "confidence", "timestamp", "score_lower", "score_upper", "coverage_guarantee")
    WALLET_FIELD_NUMBER: _ClassVar[int]
    ASSET_PAIR_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    BENFORD_FLAG_FIELD_NUMBER: _ClassVar[int]
    ML_FLAG_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    SCORE_LOWER_FIELD_NUMBER: _ClassVar[int]
    SCORE_UPPER_FIELD_NUMBER: _ClassVar[int]
    COVERAGE_GUARANTEE_FIELD_NUMBER: _ClassVar[int]
    wallet: str
    asset_pair: str
    score: int
    benford_flag: bool
    ml_flag: bool
    confidence: int
    timestamp: str
    score_lower: float
    score_upper: float
    coverage_guarantee: float
    def __init__(self, wallet: _Optional[str] = ..., asset_pair: _Optional[str] = ..., score: _Optional[int] = ..., benford_flag: _Optional[bool] = ..., ml_flag: _Optional[bool] = ..., confidence: _Optional[int] = ..., timestamp: _Optional[str] = ..., score_lower: _Optional[float] = ..., score_upper: _Optional[float] = ..., coverage_guarantee: _Optional[float] = ...) -> None: ...
