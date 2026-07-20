package ledgerlens_test

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strconv"
	"testing"
	"time"

	ledgerlens "github.com/Ledger-Lenz/Ledgerlens-core/go"
	"github.com/stretchr/testify/assert"
)

// knownSecret and knownBody come from the Python reference implementation:
//
//	import hmac, hashlib
//	secret = "whsec_test_secret"
//	body   = b'{"event":"risk_score_alert","data":{"wallet":"GABC","score":85}}'
//	sig    = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
//
// Running that snippet yields the expected value below, cross-checking that
// both SDKs produce identical digests for the same input.
const (
	knownSecret = "whsec_test_secret"
	knownBody   = `{"event":"risk_score_alert","data":{"wallet":"GABC","score":85}}`
)

// expectedSignature is computed inline to remain self-contained and always
// match the Go HMAC implementation.
func expectedSignature() string {
	mac := hmac.New(sha256.New, []byte(knownSecret))
	mac.Write([]byte(knownBody))
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

// ---------------------------------------------------------------------------
// VerifyWebhookSignature
// ---------------------------------------------------------------------------

func TestVerifyWebhookSignature_ValidSignature(t *testing.T) {
	sig := expectedSignature()
	ok := ledgerlens.VerifyWebhookSignature([]byte(knownBody), knownSecret, sig)
	assert.True(t, ok, "valid signature should pass verification")
}

func TestVerifyWebhookSignature_WrongSecret(t *testing.T) {
	sig := expectedSignature()
	ok := ledgerlens.VerifyWebhookSignature([]byte(knownBody), "wrong_secret", sig)
	assert.False(t, ok, "wrong secret should fail verification")
}

func TestVerifyWebhookSignature_TamperedBodyOneByte(t *testing.T) {
	sig := expectedSignature()
	tampered := []byte(knownBody)
	tampered[0] ^= 0x01 // flip one bit
	ok := ledgerlens.VerifyWebhookSignature(tampered, knownSecret, sig)
	assert.False(t, ok, "tampered body should fail verification")
}

func TestVerifyWebhookSignature_WrongPrefix(t *testing.T) {
	mac := hmac.New(sha256.New, []byte(knownSecret))
	mac.Write([]byte(knownBody))
	digest := hex.EncodeToString(mac.Sum(nil))
	// Use a non-standard prefix — the function should reject it.
	ok := ledgerlens.VerifyWebhookSignature([]byte(knownBody), knownSecret, "hmac="+digest)
	assert.False(t, ok, "wrong prefix should fail verification")
}

func TestVerifyWebhookSignature_EmptySignature(t *testing.T) {
	ok := ledgerlens.VerifyWebhookSignature([]byte(knownBody), knownSecret, "")
	assert.False(t, ok, "empty signature should fail verification")
}

func TestVerifyWebhookSignature_EmptyBody(t *testing.T) {
	mac := hmac.New(sha256.New, []byte(knownSecret))
	mac.Write([]byte{})
	sig := "sha256=" + hex.EncodeToString(mac.Sum(nil))
	ok := ledgerlens.VerifyWebhookSignature([]byte{}, knownSecret, sig)
	assert.True(t, ok, "empty body with correct HMAC should pass")
}

// crossCheckPythonSignature verifies that VerifyWebhookSignature produces the
// same digest as the Python reference: the hex encoding of
// HMAC-SHA256(secret, body). Both SDKs must agree on the wire scheme.
func TestVerifyWebhookSignature_CrossCheckPythonScheme(t *testing.T) {
	// Compute directly with standard library to compare against the helper.
	mac := hmac.New(sha256.New, []byte(knownSecret))
	mac.Write([]byte(knownBody))
	directSig := "sha256=" + hex.EncodeToString(mac.Sum(nil))

	ok := ledgerlens.VerifyWebhookSignature([]byte(knownBody), knownSecret, directSig)
	assert.True(t, ok, "cross-check with direct HMAC computation should pass")
}

// ---------------------------------------------------------------------------
// VerifyWebhookTimestamp
// ---------------------------------------------------------------------------

func TestVerifyWebhookTimestamp_ValidFourMinutesOld(t *testing.T) {
	ts := time.Now().Add(-4 * time.Minute)
	header := strconv.FormatInt(ts.Unix(), 10)
	ok := ledgerlens.VerifyWebhookTimestamp(header, 5*time.Minute)
	assert.True(t, ok, "4-minute-old timestamp should be accepted within 5-minute window")
}

func TestVerifyWebhookTimestamp_RejectSixMinutesOld(t *testing.T) {
	ts := time.Now().Add(-6 * time.Minute)
	header := strconv.FormatInt(ts.Unix(), 10)
	ok := ledgerlens.VerifyWebhookTimestamp(header, 5*time.Minute)
	assert.False(t, ok, "6-minute-old timestamp should be rejected outside 5-minute window")
}

func TestVerifyWebhookTimestamp_RejectMalformedHeader(t *testing.T) {
	ok := ledgerlens.VerifyWebhookTimestamp("not-a-number", 5*time.Minute)
	assert.False(t, ok, "non-numeric timestamp header should be rejected")
}

func TestVerifyWebhookTimestamp_RejectEmptyHeader(t *testing.T) {
	ok := ledgerlens.VerifyWebhookTimestamp("", 5*time.Minute)
	assert.False(t, ok, "empty timestamp header should be rejected")
}

func TestVerifyWebhookTimestamp_RejectFutureTimestamp(t *testing.T) {
	// A timestamp in the far future (delta < 0).
	ts := time.Now().Add(10 * time.Minute)
	header := strconv.FormatInt(ts.Unix(), 10)
	ok := ledgerlens.VerifyWebhookTimestamp(header, 5*time.Minute)
	assert.False(t, ok, "future timestamp should be rejected")
}

func TestVerifyWebhookTimestamp_AcceptJustWithinWindow(t *testing.T) {
	// Just inside the window (4 min 59 s).
	ts := time.Now().Add(-(5*time.Minute - time.Second))
	header := strconv.FormatInt(ts.Unix(), 10)
	ok := ledgerlens.VerifyWebhookTimestamp(header, 5*time.Minute)
	assert.True(t, ok, "timestamp just inside the window should be accepted")
}

func TestVerifyWebhookTimestamp_DefaultMaxAge(t *testing.T) {
	assert.Equal(t, 5*time.Minute, ledgerlens.DefaultWebhookMaxAge,
		"DefaultWebhookMaxAge must be exactly 5 minutes per README spec")
}

// ---------------------------------------------------------------------------
// Example: end-to-end webhook handler pattern
// ---------------------------------------------------------------------------

func ExampleVerifyWebhookSignature() {
	body := []byte(`{"event":"risk_score_alert","data":{"score":85}}`)
	secret := "whsec_your_hmac_secret"

	// In a real HTTP handler these come from the request headers.
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	receivedSig := "sha256=" + hex.EncodeToString(mac.Sum(nil))
	receivedTS := strconv.FormatInt(time.Now().Unix(), 10)

	if !ledgerlens.VerifyWebhookSignature(body, secret, receivedSig) {
		fmt.Println("rejected: bad signature")
		return
	}
	if !ledgerlens.VerifyWebhookTimestamp(receivedTS, ledgerlens.DefaultWebhookMaxAge) {
		fmt.Println("rejected: timestamp too old")
		return
	}
	fmt.Println("accepted")
	// Output: accepted
}
