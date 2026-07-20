# WAF and Adaptive Rate Limiting

This document describes the Web Application Firewall (WAF) and adaptive rate limiting features added to protect the LedgerLens API.

## Overview

The implementation includes two main components:
1. **WAF Middleware**: Protects against common web attacks (SQLi, XSS, oversized bodies, slowloris)
2. **Adaptive Rate Limiting**: Tightens rate limits automatically when abuse is detected

Both components are designed to be lightweight, configurable, and fail-open to avoid self-inflicted DoS.

## WAF Middleware

The WAF middleware is implemented in `api/waf_middleware.py` and provides the following protections:

### Features
- **SQL Injection (SQLi) Protection**: Blocks requests with known SQLi patterns in query parameters and JSON bodies
- **Cross-Site Scripting (XSS) Protection**: Blocks requests with known XSS patterns in query parameters and JSON bodies
- **Oversized Body Protection**: Rejects requests with bodies larger than `WAF_MAX_BODY_BYTES`
- **Slowloris Protection**: Times out slow requests (taking longer than `WAF_SLOW_REQUEST_TIMEOUT_SECONDS` to send the body)
- **Fail-Open Design**: If the WAF encounters an internal error, it logs the error and allows the request to proceed
- **Wallet Address Masking**: Blocked request logs have wallet addresses masked to comply with privacy requirements

### Configuration
The WAF is configurable via these environment variables (defined in `config/settings.py` and `.env.example`):
- `WAF_ENABLED`: Enable/disable the WAF (default: true)
- `WAF_MAX_BODY_BYTES`: Maximum allowed request body size in bytes (default: 1048576 / 1MB)
- `WAF_SLOW_REQUEST_TIMEOUT_SECONDS`: Timeout for slow requests in seconds (default: 10)

### Usage
The WAF middleware is automatically added to the FastAPI application in `api/main.py` and runs before other middleware.

### Endpoints
- `GET /admin/waf/blocked-requests`: Returns recent requests blocked by the WAF (admin-key gated)

## Adaptive Rate Limiting

The adaptive rate limiter is implemented in `api/adaptive_rate_limiter.py` and integrates with the existing API key rate limiting.

### Features
- **Abuse Detection**: Tracks 4xx responses and WAF blocks as abuse signals
- **Automatic Tightening**: Halves the effective rate limit for a key when the abuse threshold is reached
- **Automatic Restoration**: Gradually restores the rate limit after the abuse window passes
- **Per-Namespace/Key Awareness**: Tightening is applied per API key
- **Floor Limit**: Ensures the effective rate limit never goes below 1 request per minute

### Configuration
The adaptive rate limiter is configurable via these environment variables:
- `ADAPTIVE_RATE_TIGHTEN_FACTOR`: Factor to multiply the rate limit by when tightening (default: 0.5)
- `ADAPTIVE_RATE_ABUSE_WINDOW_SECONDS`: Time window to track abuse signals (default: 300 seconds / 5 minutes)
- `ADAPTIVE_RATE_ABUSE_THRESHOLD`: Number of abuse signals needed to trigger tightening (default: 20)

### Integration
The adaptive rate limiter is integrated into the `require_api_key_scope` dependency in `api/auth.py`, which:
1. Gets the effective rate limit using `AdaptiveNamespaceRateLimiter.effective_limit()`
2. Stores key info on the request state
3. A middleware records the response status code using `AdaptiveNamespaceRateLimiter.record_response()`

## Metrics

Two Prometheus metrics are exposed (defined in `api/metrics.py`):
- `ledgerlens_waf_blocks_total{rule, namespace_id}`: Total number of requests blocked by the WAF, grouped by rule and namespace
- `ledgerlens_adaptive_rate_limit_tightened_total{namespace_id}`: Total number of times rate limits were tightened, grouped by namespace

## Alerts

A new alert rule is added in `monitoring/alerts.yml`:
- `WAFAbuseDetected`: Triggers when more than 10 requests are blocked by the WAF in 5 minutes

## Alternative: ModSecurity + OWASP CRS

For production deployments, consider using an ingress-level WAF like ModSecurity with the OWASP Core Rule Set (CRS) instead of, or in addition to, the in-process WAF. This provides more comprehensive protection and offloads processing from the application.

### Example Nginx Configuration (simplified)
```nginx
server {
    listen 80;
    server_name api.ledgerlens.io;

    # Enable ModSecurity
    modsecurity on;
    modsecurity_rules_file /etc/nginx/modsecurity.conf;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## Testing

Tests are in `tests/test_waf_middleware.py` and cover:
- SQLi/XSS blocking
- Benign request allowance
- Oversized body blocking
- Adaptive rate limiter behavior

To run the tests:
```bash
pytest tests/test_waf_middleware.py
```

## Security Considerations
- The WAF is a defense-in-depth measure, not a substitute for proper input validation and sanitization
- Signature-based detection can have false positives; monitor blocked requests and tune if needed
- The WAF fails open to avoid self-inflicted DoS; monitor logs for internal errors
- All blocked request logs have wallet addresses masked using the existing masking logic from `config.correlation`
