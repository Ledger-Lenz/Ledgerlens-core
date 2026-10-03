package ledgerlens_test

// Cross-SDK conformance runner (Go side). Executes every case in
// tests/contract/conformance/cases.json against the shared reference server.
// Set LEDGERLENS_CONFORMANCE_URL (e.g. http://127.0.0.1:8787) to run; skipped
// otherwise. See tests/contract/conformance/README.md.

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"testing"

	ledgerlens "github.com/Ledger-Lenz/Ledgerlens-core/go"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

type conformanceCase struct {
	ID        string            `json:"id"`
	Operation string            `json:"operation"`
	Args      map[string]string `json:"args"`
	Expect    json.RawMessage   `json:"expect"`
}

func runConformanceCase(ctx context.Context, c *ledgerlens.Client, tc conformanceCase) (interface{}, error) {
	var err error
	var ok interface{}
	switch tc.Operation {
	case "health":
		var h *ledgerlens.HealthStatus
		if h, err = c.Health(ctx); err == nil {
			ok = map[string]interface{}{"status": h.Status}
		}
	case "list_scores":
		var scores []ledgerlens.RiskScore
		if scores, err = c.GetScores(ctx, tc.Args["asset_pair"]); err == nil {
			wallets, values := []string{}, []int{}
			for _, s := range scores {
				wallets = append(wallets, s.Wallet)
				values = append(values, int(s.Score))
			}
			ok = map[string]interface{}{"wallets": wallets, "scores": values}
		}
	default:
		return nil, errors.New("unknown operation " + tc.Operation)
	}
	var apiErr *ledgerlens.LedgerLensAPIError
	if errors.As(err, &apiErr) {
		return map[string]interface{}{"error": map[string]int{"status": apiErr.StatusCode}}, nil
	}
	if err != nil {
		return nil, err
	}
	return map[string]interface{}{"ok": ok}, nil
}

func TestConformance(t *testing.T) {
	baseURL := os.Getenv("LEDGERLENS_CONFORMANCE_URL")
	if baseURL == "" {
		t.Skip("LEDGERLENS_CONFORMANCE_URL not set")
	}
	raw, err := os.ReadFile("../tests/contract/conformance/cases.json")
	require.NoError(t, err)
	var doc struct {
		Cases []conformanceCase `json:"cases"`
	}
	require.NoError(t, json.Unmarshal(raw, &doc))

	client := ledgerlens.NewClient(baseURL)
	for _, tc := range doc.Cases {
		t.Run(tc.ID, func(t *testing.T) {
			resp, err := http.Post(baseURL+"/__conformance/select?case="+tc.ID, "application/json", nil)
			require.NoError(t, err)
			resp.Body.Close() //nolint:errcheck
			require.Equal(t, http.StatusOK, resp.StatusCode)

			got, err := runConformanceCase(context.Background(), client, tc)
			require.NoError(t, err)
			gotJSON, err := json.Marshal(got)
			require.NoError(t, err)
			assert.JSONEq(t, string(tc.Expect), string(gotJSON))
		})
	}
}
