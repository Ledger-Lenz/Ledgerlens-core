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
