package ledgerlens

import "time"

// RiskScore is a single wallet/asset-pair risk score returned by the
// LedgerLens scoring pipeline.
//
// Field names and units exactly match the cross-repo contract defined in
// detection/risk_score.py and the Python/TypeScript SDKs. Do not rename
// fields without updating all four language representations simultaneously.
type RiskScore struct {
	Wallet    string    `json:"wallet"`
	AssetPair string    `json:"asset_pair"`
	Score     int       `json:"score"`      // 0-100; higher = more suspicious
	BenfordFlag bool    `json:"benford_flag"`
	MLFlag    bool      `json:"ml_flag"`
	Confidence int      `json:"confidence"` // 0-100
	Disputed  bool      `json:"disputed"`
	Timestamp time.Time `json:"timestamp"`

	// Conformal prediction uncertainty fields (optional, v2+).
	// Populated when the server has a calibrated ConformalCalibrator.
	ScoreLower        *float64 `json:"score_lower,omitempty"`
	ScoreUpper        *float64 `json:"score_upper,omitempty"`
	PredictionSet     []int    `json:"prediction_set,omitempty"`
	CoverageGuarantee *float64 `json:"coverage_guarantee,omitempty"`
}

// WalletScoresResponse is the response body of GET /scores/{wallet}.
type WalletScoresResponse struct {
	Scores          []RiskScore      `json:"scores"`
	CrossChainLinks []CrossChainLink `json:"cross_chain_links"`
}

// CrossChainLink represents a confirmed EVM-wallet link for a Stellar wallet.
type CrossChainLink struct {
	Chain        string `json:"chain"`
	EVMWallet    string `json:"evm_wallet"`
	LastBridgeAt string `json:"last_bridge_at"`
}

// ShapContribution is one feature's SHAP contribution to a risk score,
// as returned by GET /scores/{wallet}/explain.
type ShapContribution struct {
	Feature   string  `json:"feature"`
	ShapValue float64 `json:"shap_value"`
}

// Ring is a detected wash-trading ring returned by GET /rings.
type Ring struct {
	ID              int      `json:"id"`
	Accounts        []string `json:"accounts"`
	TotalVolume     float64  `json:"total_volume"`
	CycleVolume     float64  `json:"cycle_volume"`
	AvgTradeCount   float64  `json:"avg_trade_count"`
	TimingTightness float64  `json:"timing_tightness"`
	DetectedAt      string   `json:"detected_at"`
}

// HealthStatus is the response body of GET /health.
type HealthStatus struct {
	Status string `json:"status"`
	DB     string `json:"db"`
	Models string `json:"models"`
}

// WebhookSubscriber is one entry in the GET /webhooks response.
type WebhookSubscriber struct {
	SubscriberID    string `json:"subscriber_id"`
	URL             string `json:"url"`
	MinScore        int    `json:"min_score"`
	WalletFilter    string `json:"wallet_filter,omitempty"`
	AssetPairFilter string `json:"asset_pair_filter,omitempty"`
	CreatedAt       string `json:"created_at"`
}

// WebhookRegisterRequest is the request body for POST /webhooks.
type WebhookRegisterRequest struct {
	URL             string `json:"url"`
	Secret          string `json:"secret"`
	MinScore        int    `json:"min_score"`
	WalletFilter    string `json:"wallet_filter,omitempty"`
	AssetPairFilter string `json:"asset_pair_filter,omitempty"`
}

// WebhookCreated is the response body of POST /webhooks.
type WebhookCreated struct {
	SubscriberID string `json:"subscriber_id"`
}
