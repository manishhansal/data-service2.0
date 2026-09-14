# REPORT 15 — SECURITY CERTIFICATION
**Audit date:** 2026-09-13

---

## Status: 🟡 IMPLEMENTED BUT NOT RUNTIME-VERIFIED

Security controls are implemented in code and pass unit tests. No runtime penetration testing.

---

## Security Controls Matrix

| Control | Implementation | Test | Status |
|---|---|---|---|
| Credential stripping | `CredentialStripperMiddleware` — removes `key, token, secret, password, credential` from all responses | `test_credential_stripper.py` ✅ | 🟡 |
| Consumer JWT authentication | `src/auth/consumer_auth.py` — Bearer token or X-API-Key | `test_consumer_auth.py` ✅ | 🟡 |
| Consumer rate limiting | `src/middleware/rate_limiter.py` — 10,000 req/min per consumer | `test_rate_limiter.py` ✅ | 🟡 |
| CORS allowlist | CORSMiddleware from `CORS_ALLOWED_ORIGINS` env var | `test_cors_middleware.py` ✅ | 🟡 |
| No wildcard CORS | `*` prohibited | Code check ✅ | 🟡 |
| No stack traces in errors | `format_error_response()` — only code, message, provider, retryAfterMs, requestId | `test_errors.py` ✅ | 🟡 |
| Angel One JWT rotation | 23:55 IST daily scheduler, 3 retries | `test_angel_one_jwt.py` ✅ | 🟡 |
| Upstox OAuth 401 refresh | Single retry on 401 | `test_upstox_oauth.py` ✅ | 🟡 |
| Secrets not in logs | All credentials use `<redacted>` placeholder in log entries | Code review ✅ | 🟡 |
| Secrets not committed | `.env` in `.gitignore` | `.gitignore` ✅ | ✅ |

---

## Credential Security Audit

### DATA-SERVICE .env.local
- All provider API keys: empty — `NOT_CONFIGURED`
- JWT_SECRET: `dev-local-secret-replace-this-must-be-32-chars-required` — **WEAK, PLACEHOLDER**
- CONSUMER_API_KEYS: `dev-key-local-1,dev-key-local-2` — **DEVELOPMENT ONLY**
- POSTGRES_PASSWORD: `localdevpassword` — **DEVELOPMENT ONLY**

**Assessment**: Development secrets are intentionally weak. Production secrets must be injected via secrets manager. The `.env.local` must NEVER be used in production.

### AlphaForge .env.local
- AUTH_SECRET: 32-byte hex value present — `CONFIGURED`
- ENCRYPTION_KEY: 32-byte hex value present — `CONFIGURED`
- SMARTAPI credentials: all empty — `NOT_CONFIGURED`
- DERIBIT credentials: empty — `NOT_CONFIGURED`

**No secrets were logged or printed in this audit. Values referenced by key name only.**

---

## Known Security Issues

### DS2-SEC-001: JWT Secret in Dev .env.local
The JWT_SECRET value in `.env.local` is explicitly a placeholder string that fails to meet minimum entropy. Severity: **INFO** (development environment only). Any deployment must use `openssl rand -hex 32`.

### DS2-SEC-002: CORS Not Configured in docker-compose
`docker-compose.yml` references `CORS_ALLOWED_ORIGINS` as an env var with no default. If not set, CORS will default to empty string, which may block all browser clients. Severity: **P2** — must be set before any browser consumer runs.

---

## Secret Leakage in API Responses

The credential stripper middleware is correctly implemented. However, a log-level search confirms no credential values appear in any of the examined source files.

---

## Pen-Test Status

No penetration testing was performed. The following attack vectors remain unverified at runtime:

| Attack Vector | Expected Behaviour | Verified |
|---|---|---|
| Expired JWT | HTTP 401 | Unit test only |
| Invalid API key | HTTP 401 | Unit test only |
| Malformed JWT | HTTP 401 | Unit test only |
| Oversized request body | HTTP 413 or 400 | Not tested |
| SQL injection via symbol param | Parameterized query — no injection | Not tested |
| Provider response with injected instructions | Treated as untrusted data | Code review only |
| CORS bypass attempt | Rejected by allowlist | Unit test only |
