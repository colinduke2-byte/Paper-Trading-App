# Hardening stream progress (branch stream/hardening)

| Finding | State |
|---|---|
| 1 Decimal bounds, field domains, validate-before-replace, broken-account isolation | done |
| 2 WebSocket validation, per-client isolation, connection caps | done |
| 3 Body limit (allow_body for larger routes), count caps, symbol cache, bounded errors/422 | done (alerts keep the alert engine's 10,000 total cap) |
| 5 News URLs http/https only (server and UI helper) | done |
| 6 Provider http: https only, no redirects, 20 MB cap, tolerant numbers + status | done |
| 7 Research types/dates/LRU-TTL cache/per-client rate limit | done |
| 8 Replay files: bounded reads, strict types, bad file listed | done |
| 9 Cross-site GET refusal, CORP, docs off by default, permissions | done; vite/vitest bump NOT done (npm 10.9.9 crashes resolving vitest 4) |
| Fuzz test, tick-stall test | done |
| Merge latest main (thread safety etc.) | in progress |

Known failing test: tests/test_orders_review.py::test_cash_account_never_ends_an_expiry_with_negative_cash_or_short_stock
(fails on main before this branch too).
