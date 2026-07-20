package ledgerlens_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	ledgerlens "github.com/Ledger-Lenz/Ledgerlens-core/go"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func newTestClient(t *testing.T, mux *http.ServeMux) (*ledgerlens.Client, *httptest.Server) {
	t.Helper()
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	client := ledgerlens.NewClient(srv.URL, ledgerlens.WithAPIKey("test-key"))
	return client, srv
}

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

func TestHealth_OK(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, http.MethodGet, r.Method)
		writeJSON(w, map[string]string{"status": "ok", "db": "ok", "models": "ok"})
	})
	client, _ := newTestClient(t, mux)

	h, err := client.Health(context.Background())
	require.NoError(t, err)
	assert.Equal(t, "ok", h.Status)
	assert.Equal(t, "ok", h.DB)
	assert.Equal(t, "ok", h.Models)
}

// ---------------------------------------------------------------------------
// GetScore
// ---------------------------------------------------------------------------

func TestGetScore_OK(t *testing.T) {
	wallet := "GABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF12345"
	mux := http.NewServeMux()
	mux.HandleFunc("/scores/"+wallet, func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, http.MethodGet, r.Method)
		assert.Equal(t, "test-key", r.Header.Get("X-LedgerLens-Admin-Key"))
		writeJSON(w, map[string]interface{}{
			"scores": []map[string]interface{}{
				{
					"wallet":       wallet,
					"asset_pair":   "XLM/USDC",
					"score":        82,
					"benford_flag": true,
					"ml_flag":      true,
					"confidence":   91,
					"disputed":     false,
					"timestamp":    "2026-07-17T12:00:00Z",
				},
			},
			"cross_chain_links": []interface{}{},
		})
	})
	client, _ := newTestClient(t, mux)

	resp, err := client.GetScore(context.Background(), wallet)
	require.NoError(t, err)
	require.Len(t, resp.Scores, 1)
	s := resp.Scores[0]
	assert.Equal(t, wallet, s.Wallet)
	assert.Equal(t, "XLM/USDC", s.AssetPair)
	assert.Equal(t, 82, s.Score)
	assert.True(t, s.BenfordFlag)
	assert.True(t, s.MLFlag)
	assert.Equal(t, 91, s.Confidence)
	assert.False(t, s.Disputed)
}

// ---------------------------------------------------------------------------
// GetScores
// ---------------------------------------------------------------------------

func TestGetScores_OK(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/scores", func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, "XLM/USDC", r.URL.Query().Get("asset_pair"))
		writeJSON(w, []map[string]interface{}{
			{
				"wallet":       "GABC",
				"asset_pair":   "XLM/USDC",
				"score":        55,
				"benford_flag": false,
				"ml_flag":      true,
				"confidence":   70,
				"disputed":     false,
				"timestamp":    "2026-07-17T12:00:00Z",
			},
		})
	})
	client, _ := newTestClient(t, mux)

	scores, err := client.GetScores(context.Background(), "XLM/USDC")
	require.NoError(t, err)
	require.Len(t, scores, 1)
	assert.Equal(t, 55, scores[0].Score)
}

func TestGetScores_NoFilter(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/scores", func(w http.ResponseWriter, r *http.Request) {
		assert.Empty(t, r.URL.Query().Get("asset_pair"))
		writeJSON(w, []interface{}{})
	})
	client, _ := newTestClient(t, mux)

	scores, err := client.GetScores(context.Background(), "")
	require.NoError(t, err)
	assert.Empty(t, scores)
}

// ---------------------------------------------------------------------------
// GetRings
// ---------------------------------------------------------------------------

func TestGetRings_OK(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/rings", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, []map[string]interface{}{
			{
				"id":               1,
				"accounts":         []string{"GAAA", "GBBB", "GCCC"},
				"total_volume":     100000.0,
				"cycle_volume":     95000.0,
				"avg_trade_count":  12.5,
				"timing_tightness": 0.03,
				"detected_at":      "2026-07-17T10:00:00Z",
			},
		})
	})
	client, _ := newTestClient(t, mux)

	rings, err := client.GetRings(context.Background())
	require.NoError(t, err)
	require.Len(t, rings, 1)
	assert.Equal(t, 1, rings[0].ID)
	assert.Equal(t, []string{"GAAA", "GBBB", "GCCC"}, rings[0].Accounts)
	assert.InDelta(t, 100000.0, rings[0].TotalVolume, 0.01)
}

// ---------------------------------------------------------------------------
// RegisterWebhook
// ---------------------------------------------------------------------------

func TestRegisterWebhook_OK(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/webhooks", func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, http.MethodPost, r.Method)
		var body ledgerlens.WebhookRegisterRequest
		require.NoError(t, json.NewDecoder(r.Body).Decode(&body))
		assert.Equal(t, "https://example.com/webhook", body.URL)
		assert.Equal(t, 75, body.MinScore)
		w.WriteHeader(http.StatusCreated)
		writeJSON(w, map[string]string{"subscriber_id": "sub-uuid-1234"})
	})
	client, _ := newTestClient(t, mux)

	created, err := client.RegisterWebhook(context.Background(), ledgerlens.WebhookRegisterRequest{
		URL:      "https://example.com/webhook",
		Secret:   "whsec_test",
		MinScore: 75,
	})
	require.NoError(t, err)
	assert.Equal(t, "sub-uuid-1234", created.SubscriberID)
}

// ---------------------------------------------------------------------------
// Error handling: 401 Unauthorized
// ---------------------------------------------------------------------------

func TestGetScore_401(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/scores/GABC", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"detail":"Invalid API key"}`))
	})
	client, _ := newTestClient(t, mux)

	_, err := client.GetScore(context.Background(), "GABC")
	require.Error(t, err)
	apiErr, ok := err.(*ledgerlens.LedgerLensAPIError)
	require.True(t, ok, "expected *LedgerLensAPIError, got %T", err)
	assert.Equal(t, http.StatusUnauthorized, apiErr.StatusCode)
	assert.Equal(t, "Invalid API key", apiErr.Detail)
}

// ---------------------------------------------------------------------------
// Error handling: 404 Not Found
// ---------------------------------------------------------------------------

func TestGetScore_404(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/scores/GNOT_FOUND", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"Wallet not found"}`))
	})
	client, _ := newTestClient(t, mux)

	_, err := client.GetScore(context.Background(), "GNOT_FOUND")
	require.Error(t, err)
	apiErr, ok := err.(*ledgerlens.LedgerLensAPIError)
	require.True(t, ok)
	assert.Equal(t, http.StatusNotFound, apiErr.StatusCode)
	assert.Equal(t, "Wallet not found", apiErr.Detail)
}

// ---------------------------------------------------------------------------
// Error handling: 429 Too Many Requests with Retry-After
// ---------------------------------------------------------------------------

func TestGetScore_429WithRetryAfter(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/scores/GABC", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "30")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"detail":"rate limit exceeded"}`))
	})
	client, _ := newTestClient(t, mux)

	_, err := client.GetScore(context.Background(), "GABC")
	require.Error(t, err)
	apiErr, ok := err.(*ledgerlens.LedgerLensAPIError)
	require.True(t, ok)
	assert.Equal(t, http.StatusTooManyRequests, apiErr.StatusCode)
	assert.Equal(t, 30*time.Second, apiErr.RetryAfter)
}

// ---------------------------------------------------------------------------
// Context cancellation
// ---------------------------------------------------------------------------

func TestGetScore_ContextCancellation(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/scores/GABC", func(w http.ResponseWriter, r *http.Request) {
		// Simulate a slow server — the context should be cancelled first.
		time.Sleep(500 * time.Millisecond)
		writeJSON(w, map[string]interface{}{"scores": []interface{}{}})
	})
	client, _ := newTestClient(t, mux)

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	_, err := client.GetScore(ctx, "GABC")
	require.Error(t, err, "expected error due to context deadline exceeded")
	// The error should wrap context.DeadlineExceeded or context.Canceled.
	assert.ErrorIs(t, err, context.DeadlineExceeded)
}

// ---------------------------------------------------------------------------
// API key is not exposed through String/GoString
// ---------------------------------------------------------------------------

func TestClient_APIKeyRedacted(t *testing.T) {
	client := ledgerlens.NewClient("https://api.ledgerlens.io", ledgerlens.WithAPIKey("super-secret-key"))
	s := client.String()
	assert.NotContains(t, s, "super-secret-key", "API key must not appear in String()")
	gs := client.GoString()
	assert.NotContains(t, gs, "super-secret-key", "API key must not appear in GoString()")
}

// ---------------------------------------------------------------------------
// RiskScore conformal prediction fields round-trip
// ---------------------------------------------------------------------------

func TestGetScore_ConformalFields(t *testing.T) {
	scoreLower := 60.0
	scoreUpper := 90.0
	coverage := 0.90
	mux := http.NewServeMux()
	mux.HandleFunc("/scores/GABC", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]interface{}{
			"scores": []map[string]interface{}{
				{
					"wallet":             "GABC",
					"asset_pair":         "XLM/USDC",
					"score":              75,
					"benford_flag":       false,
					"ml_flag":            true,
					"confidence":         80,
					"disputed":           false,
					"timestamp":          "2026-07-17T12:00:00Z",
					"score_lower":        scoreLower,
					"score_upper":        scoreUpper,
					"prediction_set":     []int{1},
					"coverage_guarantee": coverage,
				},
			},
			"cross_chain_links": []interface{}{},
		})
	})
	client, _ := newTestClient(t, mux)

	resp, err := client.GetScore(context.Background(), "GABC")
	require.NoError(t, err)
	require.Len(t, resp.Scores, 1)
	s := resp.Scores[0]
	require.NotNil(t, s.ScoreLower)
	assert.InDelta(t, 60.0, *s.ScoreLower, 0.001)
	require.NotNil(t, s.ScoreUpper)
	assert.InDelta(t, 90.0, *s.ScoreUpper, 0.001)
	assert.Equal(t, []int{1}, s.PredictionSet)
	require.NotNil(t, s.CoverageGuarantee)
	assert.InDelta(t, 0.90, *s.CoverageGuarantee, 0.001)
}
