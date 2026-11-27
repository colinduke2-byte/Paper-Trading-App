# Crypto spot trading

PaperDesk simulates spot trading in crypto pairs quoted in US dollars, next to stocks and options.
It never sends an order anywhere. With the simulated feed the coins are invented; with an Alpaca
key the prices are Alpaca's crypto market data.

## Asset classes

Every instrument has one asset class, decided by how its symbol is written
(`backend/app/core/assets.py`):

| Class | Symbol form | Example |
| --- | --- | --- |
| Stock (stocks and funds) | letters | `NVLX` |
| Option | OCC symbol | `NVLX261218C00150000` |
| Crypto | `BASE/USD` | `SIMCOIN/USD`, `BTC/USD` |

Order validation, sessions, margin, fees, settlement, the lifecycle rules and the UI all ask this
one function. The API adds `asset_class` to symbols, screener rows, positions, orders, previews and
fundamentals, and `GET /api/symbols?asset_class=crypto` and `GET /api/screener?asset_class=crypto`
filter by it. Symbols with a slash work in URL paths: `/api/bars/SIMCOIN/USD?tf=1d` (or `SIMCOIN%2FUSD`).

## Rules

| Rule | Crypto | Why |
| --- | --- | --- |
| Hours | 24 hours a day, 7 days a week, also when `PAPERDESK_SESSIONS=us` has the stock market closed | Alpaca: "Crypto trading is offered for 24 hours everyday." |
| Day orders | Expire at the UTC day roll (midnight UTC), not at the stock close | A 24/7 market has no close; UTC midnight is the simulator's day boundary |
| Order types | Market, limit, stop, stop-limit | Trailing stops are not offered for crypto here |
| Time in force | Day, good-till-canceled, immediate-or-cancel | Fill-or-kill and good-till-date are refused |
| On-open, on-close, extended hours | Refused: "on-open and on-close orders do not apply to crypto, which trades 24 hours a day" | There is no auction or session |
| Linked orders | Brackets, one-cancels-other, one-triggers-other and multi-leg orders are refused | Kept out of scope (see "Left out") |
| Quantity | A multiple of the pair's quantity increment and at least its minimum order size; up to 9 decimals | Alpaca lists `min_order_size` and `min_trade_increment` per pair |
| Dollar amount | `notional` instead of `qty` for market and limit orders: the quantity is the amount divided by the ask (buy), bid (sell) or limit price, rounded down to the increment. The fee is charged on top | |
| Prices | Limit and stop prices must be a multiple of the pair's price increment | Alpaca lists `price_increment` per pair |
| Short selling | Never, in cash and margin accounts: a sell may not exceed the holding minus open sells | Alpaca: "Cryptocurrencies can not be sold short." |
| Margin | Fully paid. In a margin account crypto has a 100% requirement (initial and maintenance), so it neither adds to nor uses stock buying power, and a crypto purchase is refused if it (with the account's other open purchases) would take cash below zero | Alpaca: "Cryptocurrencies cannot be bought on margin." |
| Margin calls | A crypto price move alone never causes a margin call (crypto counts the same in equity and in the requirement). In a margin call caused by stocks or options, liquidation may sell crypto, in the pair's quantity steps and only as much as the deficit needs. Liquidation runs only in the regular stock session, so nothing is liquidated at weekends, crypto included | |
| Settlement | Immediate (no unsettled proceeds, even with T+1 on) | |
| Pattern day trader rule | Crypto round trips are not counted, and crypto orders are not blocked by the rule | The rule covers securities |
| Dividends, splits, interest, borrow fees | Do not apply (corporate actions on a crypto symbol are ignored) | |
| Fee | A percentage of the trade value on every fill, buy or sell, taken from cash through the ledger and rounded to the cent. Default 0.25%, changeable per account in Settings ("Crypto fee", 0 to 5%) | See below |

The summary has `crypto_buying_power`: cash that can buy crypto after the fee (in a margin
account, the smaller of cash and the excess over the requirement).

### Fee simplification

Alpaca charges maker and taker fees by 30-day volume tier, starting at 0.15% maker and 0.25% taker
([crypto-fees](https://docs.alpaca.markets/us/docs/crypto-fees.md), read 2026-11-24), and takes the
fee in the asset you receive. PaperDesk uses **one rate for every fill** (0.25% by default, the
lowest-tier taker rate) and always takes it in dollars from cash, so the coin quantity you asked for
is the quantity you get. Resting limit orders are not charged a lower maker rate.

### Money precision

Cash is always kept in cents. Quantities and prices keep the pair's own precision (Decimal, up to 9
decimal places). Each fill's trade value and fee are rounded to the cent (half up), per closed lot as
for stocks, so every ledger transaction balances to the cent. A market order walks the simulated book
one price increment per level and its average price is rounded against the taker to the increment.

## Simulated coins

Real coin names with made-up prices would mislead, so the synthetic feed lists invented coins only.
Each name says "fictional coin" and the banner says the data is simulated.

| Pair | Start price | Volatility | Quantity step | Price step | Minimum |
| --- | --- | --- | --- | --- | --- |
| SIMCOIN/USD | 41,250.00 | 70% | 0.00001 | 0.01 | 0.0001 |
| SIMLEAF/USD | 2,180.50 | 85% | 0.0001 | 0.01 | 0.001 |
| SIMBYTE/USD | 0.8421 | 100% | 0.01 | 0.0001 | 1 |
| SIMDUST/USD | 0.004213 | 130% | 1 | 0.000001 | 100 |

Prices follow a seeded random walk every tick, also at weekends and while the simulated stock market is
closed. Daily history has a bar for every calendar day, so charts have no weekend gaps. The coins use
their own random generator, so adding them did not change the stock prices for a given seed. A
`market.json` saved before the coins existed still restores the stocks; the coins start fresh.

## Alpaca crypto data

With Alpaca keys, `AlpacaCryptoFeed` (`backend/app/marketdata/alpaca_crypto.py`) adds Alpaca's crypto
pairs to the stock and option feed. Facts used (read 2026-11-24):

| Fact | Source |
| --- | --- |
| REST `GET https://data.alpaca.markets/v1beta3/crypto/{loc}/snapshots`, `/latest/quotes`, `/latest/trades`, `/latest/bars`, `/latest/orderbooks`, `/bars` (`timeframe`, `start`, `end`, `limit` up to 10000 in total, `page_token`, `sort`) | [cryptosnapshots-1](https://docs.alpaca.markets/us/reference/cryptosnapshots-1.md), [cryptolatestquotes-1](https://docs.alpaca.markets/us/reference/cryptolatestquotes-1.md), [cryptobars-1](https://docs.alpaca.markets/us/reference/cryptobars-1.md), [cryptolatestorderbooks-1](https://docs.alpaca.markets/us/reference/cryptolatestorderbooks-1.md) |
| `loc`: `us` (Alpaca US), `us-1` (Kraken US), `eu-1` (Kraken EU) | same pages; [real-time-crypto-pricing-data](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data.md) |
| Quote sizes are in the base asset, with no lot multiplier | cryptolatestquotes-1 |
| Stream `wss://stream.data.alpaca.markets/v1beta3/crypto/{loc}`; same greeting, auth and error frames as the stock stream; `trades`, `quotes`, `bars`, `orderbooks` channels; `tks` is the taker side | real-time-crypto-pricing-data; [streaming-market-data](https://docs.alpaca.markets/us/docs/streaming-market-data.md) |
| Pair list `GET /v2/assets?asset_class=crypto` on the trading host, with `min_order_size`, `min_trade_increment`, `price_increment` | [crypto-trading](https://docs.alpaca.markets/us/docs/crypto-trading.md), [get-v2-assets-1](https://docs.alpaca.markets/us/reference/get-v2-assets-1.md) |
| Authentication: the OpenAPI blocks list the key headers for every crypto endpoint (one example in the docs omits them, so whether keys are needed is not settled; PaperDesk sends them) | the reference pages |
| Crypto rate limits | **Not stated** in the pages read; crypto requests share the stock budget (`PAPERDESK_ALPACA_RPM`) |

How it works: only active, tradable USD pairs are listed (cached for a day in
`alpaca_crypto_assets.json`). Like the stock side, the poller and a stream thread do all network
I/O and `step(now)` only drains a lock-protected map. Pairs that matter (open orders, positions,
open tabs, watchlists) are streamed up to 30; the rest get snapshots every 5 seconds. Charts use the
crypto bars endpoint (cached as for stocks). The depth panel shows Alpaca's latest order book
(labelled with its source) instead of a simulated one. `/api/status` has a `crypto` block with the
location, number of pairs and stream state. `PAPERDESK_ALPACA_CRYPTO_LOC` picks the location
(default `us`). Alpaca's own trading API only offers market, limit and stop-limit crypto orders with
GTC or IOC; PaperDesk is a simulator and also offers stop orders and day orders.

### What has not been tested against the live service

No Alpaca key was available, so **nothing in the crypto adapter has been run against Alpaca**. The
tests (`backend/tests/test_crypto.py`) feed it the example responses from the documentation pages
above (`backend/tests/fixtures/alpaca_crypto`, see `SOURCES.txt`). Untested in particular:

- whether the crypto endpoints accept the free Basic plan keys, and their real rate limits;
- the crypto stream with a real key (symbol limit, error codes, how often quotes arrive);
- whether the asset list's `price_increment` values match the prices the data feed sends (a pair whose
  quotes are finer than its listed increment would make limit orders at the quoted price fail the
  increment check);
- the daily bar boundary: the documented example stamps daily bars at 05:00 UTC, while PaperDesk's
  crypto day orders expire at 00:00 UTC;
- pagination of long bar histories (one page of up to 10,000 bars is read).

## Left out

- Coin-to-coin pairs (ETH/BTC) and stablecoin quotes (USDT, USDC): only `/USD` pairs are listed.
- Linked orders (brackets, OCO, OTO) and trailing stops for crypto.
- Separate maker and taker fees, volume tiers, and fees taken in the received coin.
- Alpaca's $200,000 per-order notional cap (a broker limit, not a market rule).
- Deposits and withdrawals of coins.
