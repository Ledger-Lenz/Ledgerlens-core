package ledgerlens

import (
	"crypto/tls"
	"net/http"
	"time"
)

// Option is a functional option for NewClient.
type Option func(*Client)

// WithAPIKey sets the API key sent as X-LedgerLens-Admin-Key on every request.
func WithAPIKey(key string) Option {
	return func(c *Client) {
		c.apiKey = key
	}
}

// WithHTTPClient replaces the default http.Client. Use this to set custom
// transport, proxy, or connection-pool settings.
func WithHTTPClient(hc *http.Client) Option {
	return func(c *Client) {
		c.httpClient = hc
	}
}

// WithTimeout sets the per-request timeout (default: 30 s).
func WithTimeout(d time.Duration) Option {
	return func(c *Client) {
		if c.httpClient != nil {
			c.httpClient.Timeout = d
		}
	}
}

// WithInsecureSkipVerify disables TLS certificate verification.
//
// WARNING: use only for local test servers. Never enable in production — doing
// so removes protection against MITM attacks and is rejected by security
// scanners.
func WithInsecureSkipVerify() Option {
	return func(c *Client) {
		c.httpClient = &http.Client{
			Timeout: c.httpClient.Timeout,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec // deliberate, test-only
			},
		}
	}
}

// RetryPolicy controls how the client retries failed requests.
//
// Retries are only applied to idempotent methods (GET, HEAD, DELETE), never to
// POST, and only on transport errors or HTTP 429/500/502/503/504 — the same
// status set as the shared LedgerLens Python HTTP client. Delays use
// exponential backoff with full jitter, capped at MaxBackoff; a Retry-After
// header on a 429 response takes precedence (also capped at MaxBackoff).
// Cancelling the request context aborts any pending backoff immediately.
type RetryPolicy struct {
	// MaxAttempts is the total number of attempts including the first.
	// Values <= 1 disable retries (the default).
	MaxAttempts int
	// InitialBackoff is the base delay before the first retry (default 500 ms).
	InitialBackoff time.Duration
	// MaxBackoff caps any single delay (default 30 s).
	MaxBackoff time.Duration
}

// WithRetryPolicy enables retries for idempotent requests. See RetryPolicy.
func WithRetryPolicy(p RetryPolicy) Option {
	return func(c *Client) {
		if p.InitialBackoff <= 0 {
			p.InitialBackoff = 500 * time.Millisecond
		}
		if p.MaxBackoff <= 0 {
			p.MaxBackoff = 30 * time.Second
		}
		c.retry = p
	}
}
