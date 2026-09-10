# URL Shortener

**A high-throughput URL shortener built on FastAPI, PostgreSQL, and Redis** — designed around a single principle: the redirect hot path stays fast and available even when a dependency isn't. Short-code generation is a single-query Postgres sequence lookup (no hash collisions, no retry loops), the cache layer degrades to a direct database read the moment Redis has a problem, and click analytics are recorded on a background worker so they never add latency to a redirect response.

## Core Features

- **Base62 sequence encoding** — short codes are Base62-encoded Postgres sequence values, generated in a single query with zero collision risk by construction (no probabilistic hashing, no unique-constraint retry loop).
- **302 hot-path redirects** — temporary redirects with strict `Cache-Control: no-cache, no-store, must-revalidate` headers, so every click is observed server-side instead of being silently absorbed by a browser or CDN cache.
- **Non-blocking async analytics** — click events are pushed onto an in-process queue and flushed to Postgres in batches by a background worker; the redirect response never waits on an analytics write.
- **SSRF security guards** — rejects shorten requests targeting localhost, private/link-local IP ranges, and cloud metadata endpoints (e.g. `169.254.169.254`) at creation time.
- **Token-bucket rate limiting** — atomic Redis Lua script, enforced both per-IP and per-short-code; fails **open** during a Redis outage so a cache incident never blocks legitimate traffic.
- **Graceful database fallback** — a shared circuit breaker detects Redis failures and routes the cache-aside lookup straight to Postgres with no unhandled errors; a genuine Postgres outage is isolated at the repository boundary and surfaced as a clean `503`, never a crash.

## Quick Start (One Command, Zero Local Setup)

Requires Docker and Docker Compose. No local Python, PostgreSQL, or Redis installation needed.

```bash
git clone <this-repo-url>
cd Url_shortner
docker compose up --build
```

That's it. The app waits for Postgres and Redis to report healthy before starting, creates its own schema on first boot, and is ready at **http://localhost:8000** (interactive API docs at `/docs`).

Optional: `cp .env.example .env` first if you want to override defaults (ports, rate limits, Postgres credentials) — the stack runs fine without it.

## API Quick Reference

### Create a short URL

```bash
curl -s -X POST http://localhost:8000/api/v1/shorten \
  -H "Content-Type: application/json" \
  -d '{"original_url": "https://www.anthropic.com/news/claude-code"}'
```
```json
{
  "short_code": "1",
  "short_url": "http://localhost:8000/1",
  "original_url": "https://www.anthropic.com/news/claude-code",
  "created_at": "2026-09-10T12:00:00+00:00",
  "expires_at": null
}
```

### Create a custom vanity alias

```bash
curl -s -X POST http://localhost:8000/api/v1/shorten \
  -H "Content-Type: application/json" \
  -d '{
        "original_url": "https://www.anthropic.com",
        "custom_alias": "anthropic",
        "expires_at": "2026-12-31T23:59:59Z"
      }'
```

### Follow a short link

```bash
curl -i http://localhost:8000/anthropic
```
```
HTTP/1.1 302 Found
location: https://www.anthropic.com
cache-control: no-cache, no-store, must-revalidate
```

### Get click analytics

```bash
curl -s http://localhost:8000/api/v1/urls/anthropic/analytics
```
```json
{
  "short_code": "anthropic",
  "total_clicks": 3,
  "time_series": [{ "bucket": "2026-09-10T00:00:00+00:00", "clicks": 3 }],
  "top_referrers": [{ "referrer": null, "clicks": 3 }]
}
```

Optional query params: `granularity` (`hour` | `day`, default `day`), `from`, `to` (ISO 8601, default last 7 days).

## Testing Gate

Two layers of automated verification exist and must both pass before a change ships:

```bash
# Unit + integration test suite (spins up ephemeral, real Postgres/Redis)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest

# End-to-end black-box verification against a live Docker deployment
./test_stack.sh
```

`test_stack.sh` drives the actual containerized stack through startup, the create → redirect flow, alias conflicts, SSRF rejection, analytics propagation, and a live Redis-outage fallback test — see `QA_CHECKLIST.md` for the manual sign-off checklist covering the same ground.
