package ledgerlens_test

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"time"

	ledgerlens "github.com/Ledger-Lenz/Ledgerlens-core/go"
)

// scoresResponse is a canned GET /scores/{wallet} body used by the examples so
// they are fully runnable (and verifiable) via `go test` without a live API.
const scoresResponse = `{
  "scores": [
    {
      "wallet": "GABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF12345",
      "asset_pair": "XLM/USDC",
      "score": 82,
      "benford_flag": true,
      "ml_flag": true,
      "confidence": 91,
      "disputed": false,
      "timestamp": "2026-07-17T12:00:00Z"
    }
  ],
  "cross_chain_links": []
}`

// Example shows the basic usage of the Go SDK: construct a client with an API
// key and fetch the risk scores for a wallet.
//
// In real code, pass the production base URL ("https://api.ledgerlens.io")
// instead of the local test server used here.
func Example() {
	// Stand-in for the LedgerLens API. A real program would not need this.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if _, err := io.WriteString(w, scoresResponse); err != nil {
			panic(err)
		}
	}))
	defer srv.Close()

	client := ledgerlens.NewClient(
		srv.URL, // use "https://api.ledgerlens.io" in production
		ledgerlens.WithAPIKey("your-api-key"),
	)

	resp, err := client.GetScore(context.Background(), "GABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF12345")
	if err != nil {
		fmt.Println("lookup failed:", err)
		return
	}

	for _, s := range resp.Scores {
		fmt.Printf("%s score=%d benford=%v ml=%v\n", s.AssetPair, s.Score, s.BenfordFlag, s.MLFlag)
	}
	// Output: XLM/USDC score=82 benford=true ml=true
}

// Example_withdrawalGating demonstrates the common exchange-backend pattern of
// blocking a withdrawal when a wallet's risk score is at or above a threshold
// and the ML classifier has flagged it.
func Example_withdrawalGating() {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if _, err := io.WriteString(w, scoresResponse); err != nil {
			panic(err)
		}
	}))
	defer srv.Close()

	client := ledgerlens.NewClient(srv.URL, ledgerlens.WithAPIKey("your-api-key"))

	allowed := true
	resp, err := client.GetScore(context.Background(), "GABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF12345")
	if err != nil {
		fmt.Println("lookup failed:", err)
		return
	}
	for _, s := range resp.Scores {
		if s.Score >= 70 && s.MLFlag {
			allowed = false
		}
	}

	fmt.Println("withdrawal allowed:", allowed)
	// Output: withdrawal allowed: false
}

// ExampleVerifySignature shows verifying an inbound LedgerLens webhook inside
// an http.Handler using the X-LedgerLens-Signature and X-LedgerLens-Timestamp
// headers set by the API.
func ExampleVerifySignature() {
	const secret = "whsec_test_secret"
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		if err := ledgerlens.VerifySignature(
			body,
			secret,
			r.Header.Get("X-LedgerLens-Signature"),
			r.Header.Get("X-LedgerLens-Timestamp"),
			ledgerlens.DefaultWebhookMaxAge,
		); err != nil {
			http.Error(w, err.Error(), http.StatusUnauthorized)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})

	// Simulate the API delivering a webhook (signature from api/webhook_sender.py).
	body := `{"event":"risk_score_alert","wallet":"GABC","score":87}`
	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(body))
	req.Header.Set("X-LedgerLens-Signature", "sha256=2e1b1d2a6dbb4597dc74eab9b8ffbc8e1ab290fa529280569fa84789d53ef66c")
	req.Header.Set("X-LedgerLens-Timestamp", strconv.FormatInt(time.Now().Unix(), 10))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	fmt.Println(rec.Code)

	req.Header.Set("X-LedgerLens-Signature", "sha256=deadbeef")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	fmt.Println(rec.Code)
	// Output:
	// 204
	// 401
}
