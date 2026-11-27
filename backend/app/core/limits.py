"""Every size and count limit in one place. docs/LIMITS.md explains each one.

PaperDesk is a single-user program, but in LAN mode a signed-in phone (or a hostile page, when the
loopback bypass is on) can send anything. These limits keep one request, one connection or one
damaged file from using up memory, disk or the tick loop. Numbers themselves are bounded in
app.core.money.parse (length, magnitude, decimal places).

A limit is reached with a plain message naming it, never a 500.
"""

KB = 1024
MB = 1024 * KB

# ---- HTTP requests --------------------------------------------------------------------------------
MAX_BODY_BYTES = 64 * KB          # default request body limit for every /api route
MAX_ERROR_TEXT = 300              # characters of an error message (inputs are never echoed in full)
MAX_ECHO_TEXT = 40                # characters of a user value quoted inside an error message
MAX_VALIDATION_ERRORS = 10        # entries in a 422 validation answer

# ---- accounts and their contents ------------------------------------------------------------------
MAX_ACCOUNTS = 20
MAX_WORKING_ORDERS = 500          # open (working, held, partly filled) orders per account
MAX_JOURNAL_ENTRIES = 2000        # per account
MAX_JOURNAL_NOTE = 4000           # characters
MAX_ALERTS = 10_000               # alert rules in total (app/alerts; its tick cost is bounded by test_alerts)
MAX_WATCHLISTS = 20
MAX_WATCHLIST_SYMBOLS = 200
MAX_WATCHLIST_NAME = 30

# ---- chart settings and drawings (routes_chartstate.py) -------------------------------------------------
MAX_CHART_RECORD = 256 * KB       # one settings or drawings record, as JSON
MAX_CHART_BODY = 320 * KB         # request body limit for the two chart-state PUT routes
MAX_CHART_TOTAL = 8 * MB          # all chart records together (the file is rewritten on every save)

# ---- symbols ----------------------------------------------------------------------------------------
MAX_SYMBOL_TEXT = 32              # longest symbol text looked at; OCC option symbols are 21 at most

# ---- WebSocket ----------------------------------------------------------------------------------------
MAX_WS_MESSAGE = 16 * KB          # characters in one message from a browser
MAX_WS_SYMBOLS = 300              # subscribed symbols per connection
MAX_WS_CONNECTIONS = 32           # open streams in total
MAX_WS_PER_ADDRESS = 8            # open streams from one client address
WS_CLOSE_POLICY = 1008            # close code: the client broke a rule (bad message, too many streams)
WS_CLOSE_TOO_BIG = 1009           # close code: message too large

# ---- research (provider-backed GETs) --------------------------------------------------------------------
RESEARCH_CACHE_ROWS = 256         # entries kept; least recently used go first
RESEARCH_CACHE_MAX_AGE = 6 * 3600  # seconds; older entries are dropped even if never used again
RESEARCH_MAX_RANGE_DAYS = 730     # corporate-action date ranges are clamped to this span
RESEARCH_RATE_PER_MINUTE = 60     # provider-backed research requests per client per minute
RESEARCH_RATE_BURST = 20

# ---- provider responses ----------------------------------------------------------------------------------
MAX_PROVIDER_RESPONSE = 20 * MB
PROVIDER_TIMEOUT = 15.0           # seconds, connect and each read
MAX_PROVIDER_PRICE = 10_000_000   # a quoted price above this is treated as invalid data

# ---- replay session files --------------------------------------------------------------------------------
MAX_SESSION_LINE = 8 * MB         # characters in one line of a session file (context lines hold many bars)
MAX_SESSION_HEADER = 1 * MB       # characters in the header line (read for every listing)
MAX_SESSION_SYMBOLS = 500
