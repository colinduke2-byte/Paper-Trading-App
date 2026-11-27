# Limits and hardening

PaperDesk is a single-user program, but in LAN mode a signed-in phone can send anything, and a page open in
your browser can send requests while the loopback bypass is on. These limits stop one request, one connection
or one damaged file from using up memory or disk, or stalling the market clock. Each one refuses with a plain
message that names the limit, never with a server error.

Every limit is a constant in `backend/app/core/limits.py`. Numbers are bounded in `backend/app/core/money.py`.

## Numbers

Every number that arrives from outside (request bodies and query strings) goes through `money.parse`, which
refuses:

| Input | Answer |
|---|---|
| missing, `true`/`false`, a list or an object | "... must be a number" or "... is missing" |
| text longer than 40 characters | "... is too long" |
| NaN or infinity | "... must be a finite number" |
| an absolute value above 1,000,000,000,000 | "... is too large" |
| more than 8 decimal places | "... has more than 8 decimal places" (a JSON float such as `0.30000000000000004` is rounded to 8 places instead, because the extra digits are a binary artefact) |

Each field then has its own range:

| Field | Range |
|---|---|
| Starting cash (new account, reset, replay) | more than 0, at most 1,000,000,000, whole cents. A reset is checked completely before anything is replaced |
| Order prices (limit, stop, take profit, stop loss, trail amount) | positive, at most 1,000,000,000, on the tick grid: whole cents at or above $1, hundredths of a cent below. Changing an order applies the same rules |
| Alert levels | within the bounds above and at most 1,000,000,000 (a level need not be on the tick grid) |
| Trail percent | above 0 and below 100, at most 4 decimal places |
| Quantity | as before: positive, at most 1,000,000, at most 4 decimal places, whole contracts for options |
| Rule settings (interest rates, grace hours, equity minimum) | the ranges shown in the Settings dialog |

## Accounts that cannot be valued

If one account cannot be valued (for example an impossible number in a state file written by an older
version), it is listed with `"broken": true` and an explanation instead of failing the account list, and the
tick, the margin checks, the interest accrual and the live stream carry on for every other account. The app
shows the account with "needs attention" and offers to reset it with new starting cash or delete it.

## Requests

| Limit | Value |
|---|---|
| Request body for any `/api` route | 64 KB (Content-Length is checked first; a body without one is counted as it arrives) |
| Chart settings and drawings (`PUT /api/chartstate/...`) | 320 KB per request, 256 KB per record, 8 MB in total |
| Sign-in request | 4 KB |
| Error messages | at most 300 characters; values from the request are never echoed in full |
| Validation errors (HTTP 422) | at most 10 entries, without the submitted input |

A route that needs a bigger body declares it once when it registers:

```python
from .hardening import allow_body
allow_body(app, "/api/backup/restore", 50 * limits.MB)     # an exact path or a route template
```

## Counts

| What | Limit |
|---|---|
| Accounts | 20 |
| Open orders per account | 500 |
| Orders in one linked group, legs in a multi-leg order | 5 and 4 (unchanged) |
| Alert rules | 10,000 in total (the alert engine only evaluates rules whose symbol changed, so tick cost stays bounded) |
| Watchlists | 20, with at most 200 symbols each; names up to 30 characters |
| Journal entries | 2,000 per account; a note up to 4,000 characters, 12 tags of up to 32 characters |
| Symbol text | 32 characters; longer text is refused before any lookup or cache (option symbols are at most 21) |

## Live stream (WebSocket)

| Limit | Value |
|---|---|
| Open streams | 32 in total, 8 per client address; more are refused at the handshake |
| Message from a browser | 16 KB; a JSON object with `symbols` (a list of short strings, the first 300 kept) and `account` (an id or null) |
| A message that breaks these rules | the stream is closed with code 1008 (1009 when too large, 1003 for binary) |

Each browser's tick message is built and sent on its own: one bad or slow connection (5 s to accept a message)
is dropped without affecting the others or the events they receive.

## Research and real market data

* Research requests that use a provider (news, calendar, corporate actions, fundamentals) are limited to 60 a
  minute per client, in bursts of up to 20 (HTTP 429 with `Retry-After`).
* Corporate-action `types` must be known record types; date ranges are kept within 730 days, five years back
  and one year ahead. The research cache keeps at most 256 entries for at most 6 hours.
* Provider addresses must be `https://`. Redirects are never followed (the keys travel in the request headers).
  Responses are limited to 20 MB, with a 15 s timeout per read and an overall deadline.
* A provider number that cannot be read, is not finite, is negative or above 10,000,000 (for a price) is ignored
  and counted. The status banner then says "feed sent invalid data" with the count.
* News links are kept only when they are `http` or `https` URLs; any other link is shown as plain text. The UI
  checks every data-driven link again (`frontend/src/safeUrl.ts`).

## Replay session files

A session file is read with bounded lines (1 MB for the header, 8 MB for any other line), at most 500 symbols,
and strict types: every price must be a finite positive number and every size a finite non-negative one. A file
that breaks a rule is listed with its error and cannot be replayed; it never breaks the session list.

## Browser and API documentation

* `/api` GET requests that a browser marks `Sec-Fetch-Site: cross-site` are refused, and every response carries
  `Cross-Origin-Resource-Policy: same-origin`.
* FastAPI's `/docs`, `/redoc` and `/openapi.json` are off. They load scripts from a CDN, which the content
  security policy does not allow. Developers can turn them on with `PAPERDESK_API_DOCS=1`.

## File permissions

The data directory holds your accounts and sign-in digests, and `.env` can hold the passcode and provider keys.

* **Linux and macOS:** PaperDesk creates a new data directory with mode `0700` (yours only). Saved files are
  written through temporary files created with mode `0600`, and `sessions.json` is `0600`. At start-up it warns
  when the data directory or `.env` can be read by other users, and prints the `chmod` command that fixes it
  (`chmod 700 backend/data`, `chmod 600 .env`). It does not change existing permissions itself.
* **Windows:** files under your user profile (where PaperDesk normally lives) are private to your account by
  default, and Python cannot express POSIX modes there, so nothing is checked. If you keep PaperDesk in a shared
  folder such as `C:\Users\Public`, move it into your own profile.
