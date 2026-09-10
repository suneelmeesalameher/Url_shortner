# URL Shortener - Containerized Stack QA Checklist

Companion to `test_stack.sh`, which automates every check below. Run the script
for a pass/fail result in ~30-60 seconds; use this checklist for a manual
walkthrough or as a sign-off record attached to a release.

**Prerequisites:** Docker + Docker Compose v2, `curl`, `python3`. Run from the
repository root (where `docker-compose.yml` lives).

```bash
./test_stack.sh
```

---

## 1. Docker Compose Startup

- [ ] `docker compose up --build -d` exits with status 0
- [ ] `docker compose ps` shows `app`, `postgres`, and `redis` all `Up`
- [ ] All three containers report `(healthy)` in `docker compose ps` (not just `Up`) within 90s
- [ ] `curl -i http://localhost:8000/healthz` returns **200 OK** with body `{"status":"ok"}`

## 2. Greenfield Flow

- [ ] `POST /api/v1/shorten` with a fresh `original_url` returns **201 Created**
- [ ] Response body contains a non-empty `short_code` and a `short_url` ending in `/{short_code}`
- [ ] `GET /{short_code}` returns **302 Found**
- [ ] The `Location` response header exactly matches the original `original_url`
- [ ] The response includes `Cache-Control: no-cache, no-store, must-revalidate` (so browsers/CDNs can't cache the redirect and silently undercount future analytics)

## 3. Custom Alias & Conflict Check

- [ ] `POST /api/v1/shorten` with a `custom_alias` returns **201 Created**, and `short_code` equals the requested alias exactly
- [ ] A second `POST` reusing the **same** `custom_alias` (any `original_url`) returns **409 Conflict**
- [ ] The 409 response body has `"error": "ALIAS_TAKEN"`
- [ ] No second row was created - `GET /{alias}` still redirects to the **first** request's `original_url`, not the second

## 4. SSRF Guard Validation

- [ ] `POST` with `original_url: "http://169.254.169.254/latest/meta-data"` (cloud metadata endpoint) is rejected with **400** (or 422)
- [ ] `POST` with `original_url: "http://127.0.0.1/"` (loopback) is rejected with **400** (or 422)
- [ ] `POST` with `original_url: "http://localhost/"` is rejected
- [ ] `POST` with `original_url: "http://10.0.0.1/"` (RFC1918 private range) is rejected
- [ ] None of the above created a row - re-running the same request doesn't return a `short_code` from a prior attempt

## 5. Analytics Reporting

- [ ] `GET /{short_code}` a known number of times (e.g. 3), then `GET /api/v1/urls/{short_code}/analytics` returns **200 OK**
- [ ] `total_clicks` eventually equals the number of redirects performed (allow a few seconds - the click recorder flushes asynchronously in batches, not per-request)
- [ ] `time_series` is non-empty and its bucket counts sum to `total_clicks`
- [ ] `top_referrers` is present in the response shape (may be `[{"referrer": null, "clicks": N}]` for `curl`-driven traffic with no `Referer` header)

## 6. Fallback & Resiliency Test

- [ ] `docker compose stop redis` succeeds
- [ ] `GET /{short_code}` (for a code created *before* Redis was stopped) still returns **302 Found** with the correct `Location` header
- [ ] The request completes quickly (well under the 5s curl timeout) - it should **not** hang waiting on a dead Redis connection
- [ ] No 500 error, no dropped connection, no stack trace in `docker compose logs app`
- [ ] `docker compose logs app` shows a structured (JSON) warning log line for the failed Redis call, not a silent failure
- [ ] `docker compose start redis` brings Redis back; `docker compose ps` reports it `(healthy)` again shortly after
- [ ] A subsequent `GET /{short_code}` still works (now served from Postgres again, cache re-populating)

---

## Sign-off

| Field | Value |
|---|---|
| Date | |
| Tester | |
| `test_stack.sh` result | ☐ All passed ☐ Failures (see below) |
| Notes / failures | |
