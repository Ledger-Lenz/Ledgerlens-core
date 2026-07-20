package ledgerlens

import (
	"fmt"
	"net/http"
	"strconv"
	"time"
)

// LedgerLensAPIError is returned when the server responds with a non-2xx
// status code. Callers can type-assert to inspect the status, body, and
// Retry-After header (set on HTTP 429 responses).
type LedgerLensAPIError struct {
	// StatusCode is the HTTP status code (e.g. 401, 404, 429).
	StatusCode int
	// Detail is the parsed "detail" field from the JSON error body, or the
	// raw response text when the body is not valid JSON.
	Detail string
	// RetryAfter is the parsed value of the Retry-After header. It is only
	// set on 429 responses. Zero value means the header was absent or could
	// not be parsed.
	RetryAfter time.Duration
}

func (e *LedgerLensAPIError) Error() string {
	if e.RetryAfter > 0 {
		return fmt.Sprintf("ledgerlens API error %d: %s (retry after %s)", e.StatusCode, e.Detail, e.RetryAfter)
	}
	return fmt.Sprintf("ledgerlens API error %d: %s", e.StatusCode, e.Detail)
}

// newAPIError constructs a LedgerLensAPIError from an HTTP response and the
// already-consumed response body text.
func newAPIError(resp *http.Response, body string) *LedgerLensAPIError {
	err := &LedgerLensAPIError{
		StatusCode: resp.StatusCode,
		Detail:     extractDetail(body),
	}
	if resp.StatusCode == http.StatusTooManyRequests {
		if ra := resp.Header.Get("Retry-After"); ra != "" {
			if secs, parseErr := strconv.ParseFloat(ra, 64); parseErr == nil {
				err.RetryAfter = time.Duration(secs * float64(time.Second))
			}
		}
	}
	return err
}

// extractDetail pulls the "detail" string out of a JSON error body like
// {"detail": "Not found"}. Falls back to the raw body on parse failure.
func extractDetail(body string) string {
	// Minimal JSON extraction: avoid importing encoding/json only for this.
	// A proper unmarshal happens in the client where needed.
	const prefix = `"detail":`
	idx := indexOf(body, prefix)
	if idx < 0 {
		if body == "" {
			return "unknown error"
		}
		return body
	}
	rest := body[idx+len(prefix):]
	// Skip whitespace and opening quote.
	for len(rest) > 0 && (rest[0] == ' ' || rest[0] == '\t') {
		rest = rest[1:]
	}
	if len(rest) == 0 || rest[0] != '"' {
		return body
	}
	rest = rest[1:]
	end := indexOf(rest, `"`)
	if end < 0 {
		return body
	}
	return rest[:end]
}

// indexOf is strings.Index without importing "strings" at package level
// to keep this file dependency-free.
func indexOf(s, sub string) int {
	if len(sub) == 0 {
		return 0
	}
	for i := 0; i <= len(s)-len(sub); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
