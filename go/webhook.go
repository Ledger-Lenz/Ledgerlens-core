package ledgerlens

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"strings"
	"time"
)

// DefaultWebhookMaxAge is the recommended maximum age for accepted webhook
// timestamps (5 minutes), matching the README's "SHOULD reject timestamps
// older than 5 minutes" guidance.
const DefaultWebhookMaxAge = 5 * time.Minute

// VerifyWebhookSignature reports whether the HMAC-SHA256 signature in
// signature matches the expected digest of body using secret.
//
// The signature parameter must have the form "sha256=<hex-digest>", which is
// the exact format sent in the X-LedgerLens-Signature header.
//
// SECURITY: comparison is performed with hmac.Equal (constant-time). Never
// compare webhook signatures with == or bytes.Equal — those operations are
// vulnerable to timing side-channel attacks.
//
// This implementation is the direct Go equivalent of the Python reference in
// README.md and docs/webhook_security_model.md:
//
//	import hmac, hashlib
//	expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
//	return hmac.compare_digest(signature, expected)
func VerifyWebhookSignature(body []byte, secret, signature string) bool {
	if !strings.HasPrefix(signature, "sha256=") {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	expected := "sha256=" + hex.EncodeToString(mac.Sum(nil))
	// hmac.Equal is constant-time; this is the correct comparison function.
	return hmac.Equal([]byte(expected), []byte(signature))
}

// VerifyWebhookTimestamp reports whether the Unix-epoch-second timestamp in
// timestampHeader falls within maxAge of the current wall-clock time.
//
// Returns false when:
//   - timestampHeader is empty or not a valid integer
//   - the timestamp is in the future (delta < 0)
//   - the timestamp is older than maxAge
//
// Pass DefaultWebhookMaxAge (5 minutes) unless your use case requires a
// different replay-prevention window. The README specifies 5 minutes as the
// recommended rejection threshold.
func VerifyWebhookTimestamp(timestampHeader string, maxAge time.Duration) bool {
	ts, err := strconv.ParseInt(strings.TrimSpace(timestampHeader), 10, 64)
	if err != nil {
		return false
	}
	delta := time.Since(time.Unix(ts, 0))
	return delta >= 0 && delta <= maxAge
}
