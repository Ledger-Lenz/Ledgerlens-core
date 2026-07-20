// Package ledgerlens provides an idiomatic Go client for the LedgerLens
// fraud-detection REST API.
//
// # Usage
//
//	client := ledgerlens.NewClient("https://api.ledgerlens.io",
//	    ledgerlens.WithAPIKey("your-api-key"),
//	)
//	resp, err := client.GetScore(ctx, wallet)
//	if err != nil { return err }
//	for _, s := range resp.Scores {
//	    if s.Score >= 70 && s.MLFlag {
//	        return fmt.Errorf("withdrawal blocked: risk score %d", s.Score)
//	    }
//	}
//
// # Webhook verification
//
//	ok := ledgerlens.VerifyWebhookSignature(body, secret, r.Header.Get("X-LedgerLens-Signature"))
//	if !ok {
//	    http.Error(w, "invalid signature", http.StatusUnauthorized)
//	    return
//	}
//	ok = ledgerlens.VerifyWebhookTimestamp(r.Header.Get("X-LedgerLens-Timestamp"), 5*time.Minute)
//
// # Struct types
//
// All structs intentionally mirror (but do not import from) detection/risk_score.py
// and the Python SDK models. Keep them in sync with the cross-repo contract
// described in the root README's "LedgerLens Organization" section.
package ledgerlens
