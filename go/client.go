package ledgerlens

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	defaultTimeout = 30 * time.Second
	userAgent      = "ledgerlens-go-sdk/0.1.0"
)

// Client is the main LedgerLens API client. Every method accepts a
// context.Context so callers can apply timeouts and cancellation.
//
// Client must be constructed with NewClient; a zero-value Client is not valid.
//
// The apiKey field is redacted from all string representations to prevent
// accidental leakage in logs.
type Client struct {
	baseURL    string
	apiKey     string // never exposed through String/GoString/log
	httpClient *http.Client
}

// String returns a non-sensitive representation of the client.
// The API key is redacted.
func (c *Client) String() string {
	return fmt.Sprintf("ledgerlens.Client{baseURL: %q, apiKey: [REDACTED]}", c.baseURL)
}

// GoString is the %#v representation. The API key is redacted.
func (c *Client) GoString() string {
	return c.String()
}

// NewClient constructs a Client targeting baseURL with the given options.
//
// baseURL should be the scheme+host only (e.g. "https://api.ledgerlens.io").
// No trailing slash is required; the client normalises it internally.
//
//	client := ledgerlens.NewClient("https://api.ledgerlens.io",
//	    ledgerlens.WithAPIKey(apiKey),
//	)
func NewClient(baseURL string, opts ...Option) *Client {
	c := &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		httpClient: &http.Client{
			Timeout: defaultTimeout,
		},
	}
	for _, o := range opts {
		o(c)
	}
	return c
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

// Health calls GET /health and returns the system status.
func (c *Client) Health(ctx context.Context) (*HealthStatus, error) {
	var out HealthStatus
	if err := c.get(ctx, "/health", nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ---------------------------------------------------------------------------
// Scores
// ---------------------------------------------------------------------------

// GetScore calls GET /scores/{wallet} and returns all risk scores recorded for
// that wallet across every asset pair, plus any confirmed cross-chain links.
func (c *Client) GetScore(ctx context.Context, wallet string) (*WalletScoresResponse, error) {
	var out WalletScoresResponse
	if err := c.get(ctx, "/scores/"+url.PathEscape(wallet), nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetScores calls GET /scores with an optional asset_pair filter and returns
// a list of RiskScore records.
func (c *Client) GetScores(ctx context.Context, assetPair string) ([]RiskScore, error) {
	var params map[string]string
	if assetPair != "" {
		params = map[string]string{"asset_pair": assetPair}
	}
	var out []RiskScore
	if err := c.get(ctx, "/scores", params, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// ExplainScore calls GET /scores/{wallet}/explain and returns per-feature
// SHAP contributions for the given wallet/asset-pair combination.
// An admin key (WithAPIKey) is required.
func (c *Client) ExplainScore(ctx context.Context, wallet, assetPair string) ([]ShapContribution, error) {
	params := map[string]string{"asset_pair": assetPair}
	var out []ShapContribution
	if err := c.get(ctx, "/scores/"+url.PathEscape(wallet)+"/explain", params, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// Rings
// ---------------------------------------------------------------------------

// GetRings calls GET /rings and returns the list of currently detected
// wash-trading rings.
func (c *Client) GetRings(ctx context.Context) ([]Ring, error) {
	var out []Ring
	if err := c.get(ctx, "/rings", nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// Webhooks
// ---------------------------------------------------------------------------

// RegisterWebhook calls POST /webhooks to register a new webhook subscriber.
func (c *Client) RegisterWebhook(ctx context.Context, req WebhookRegisterRequest) (*WebhookCreated, error) {
	var out WebhookCreated
	if err := c.post(ctx, "/webhooks", req, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListWebhooks calls GET /webhooks and returns the active subscribers.
// An admin key (WithAPIKey) is required.
func (c *Client) ListWebhooks(ctx context.Context) ([]WebhookSubscriber, error) {
	var out []WebhookSubscriber
	if err := c.get(ctx, "/webhooks", nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// DeleteWebhook calls DELETE /webhooks/{subscriberID} to deactivate a
// webhook subscriber.
// An admin key (WithAPIKey) is required.
func (c *Client) DeleteWebhook(ctx context.Context, subscriberID string) error {
	req, err := c.newRequest(ctx, http.MethodDelete, "/webhooks/"+url.PathEscape(subscriberID), nil)
	if err != nil {
		return err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close() //nolint:errcheck
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return newAPIError(resp, string(body))
	}
	return nil
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

// get executes a GET request, decodes the JSON response into out.
func (c *Client) get(ctx context.Context, path string, params map[string]string, out interface{}) error {
	req, err := c.newRequest(ctx, http.MethodGet, path, nil)
	if err != nil {
		return err
	}
	if len(params) > 0 {
		q := req.URL.Query()
		for k, v := range params {
			q.Set(k, v)
		}
		req.URL.RawQuery = q.Encode()
	}
	return c.do(req, out)
}

// post executes a POST request with a JSON body, decodes the JSON response.
func (c *Client) post(ctx context.Context, path string, body interface{}, out interface{}) error {
	encoded, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("ledgerlens: marshal request: %w", err)
	}
	req, err := c.newRequest(ctx, http.MethodPost, path, strings.NewReader(string(encoded)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	return c.do(req, out)
}

// newRequest builds an *http.Request with the correct base URL, User-Agent,
// and (optionally) the API key header. The API key is never written to logs.
func (c *Client) newRequest(ctx context.Context, method, path string, body io.Reader) (*http.Request, error) {
	fullURL := c.baseURL + path
	req, err := http.NewRequestWithContext(ctx, method, fullURL, body)
	if err != nil {
		return nil, fmt.Errorf("ledgerlens: build request: %w", err)
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", "application/json")
	if c.apiKey != "" {
		req.Header.Set("X-LedgerLens-Admin-Key", c.apiKey)
	}
	return req, nil
}

// do executes the request, checks the status, and decodes the JSON body.
func (c *Client) do(req *http.Request, out interface{}) error {
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close() //nolint:errcheck
	rawBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("ledgerlens: read response body: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return newAPIError(resp, string(rawBody))
	}
	if out != nil {
		if err := json.Unmarshal(rawBody, out); err != nil {
			return fmt.Errorf("ledgerlens: decode response: %w", err)
		}
	}
	return nil
}
