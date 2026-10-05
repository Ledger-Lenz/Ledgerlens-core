package ledgerlens

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strconv"
	"strings"
	"time"
)

// DefaultWebhookMaxAge is the recommended maximum age for accepted webhook
// timestamps (5 minutes), matching the README's "SHOULD reject timestamps
// older than 5 minutes" guidance.
const DefaultWebhookMaxAge = 5 * time.Minute

// Errors returned by VerifySignature.
var (
	// ErrInvalidWebhookSignature means X-LedgerLens-Signature did not match the body.
	ErrInvalidWebhookSignature = errors.New("ledgerlens: invalid webhook signature")
	// ErrStaleWebhookTimestamp means X-LedgerLens-Timestamp was missing,
	// malformed, in the future, or older than the allowed max age.
	ErrStaleWebhookTimestamp = errors.New("ledgerlens: stale or invalid webhook timestamp")
)

// VerifySignature verifies an inbound webhook exactly as api/webhook_sender.py
// signs it: signatureHeader (X-LedgerLens-Signature) must equal
// "sha256=" + hex(HMAC-SHA256(secret, body)), and timestampHeader
// (X-LedgerLens-Timestamp) must be within maxAge of now. Pass
// DefaultWebhookMaxAge unless you need a different replay window.
//
// body must be the raw, unmodified request body. The signature comparison is
// constant-time (hmac.Equal). Returns nil on success, otherwise
// ErrInvalidWebhookSignature or ErrStaleWebhookTimestamp.
func VerifySignature(body []byte, secret, signatureHeader, timestampHeader string, maxAge time.Duration) error {
	if !VerifyWebhookSignature(body, secret, signatureHeader) {
		return ErrInvalidWebhookSignature
	}
	if !VerifyWebhookTimestamp(timestampHeader, maxAge) {
		return ErrStaleWebhookTimestamp
	}
	return nil
}

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
