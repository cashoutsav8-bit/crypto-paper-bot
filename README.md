# crypto-paper-bot

Paper-trading research bot for Coinbase nano perpetual futures. **Paper only: it never places orders and uses no API keys.** It reads Coinbase's public market data, simulates each strategy, and publishes a dashboard with GitHub Pages.

## Strategies

| | Strategy | Paper capital | Backtest (2018-22 / 2023-26 Sharpe) |
|---|---|---|---|
| A | Trend + ATR stops on BTC, ETH, SOL, XRP. Enter when close > close 20 days ago; stop 2 ATR below entry, trailing 3 ATR below the high. 5% risk per trade, 15% max risk to starting capital. | $2,000 | +0.48R / +0.32R per trade |
| A2 | Same as A, but when a trade is up 4%, sell half and move the stop on the rest to break-even. 2.5% risk per trade from 2026-10-07 (was 5%). | $5,000 | +0.09R per trade, 71% win rate (8-yr test) |
| S | Bear-market short: mirror of A on the short side, only while BTC is below its 50-day average. 2.5% risk/trade. | $5,000 | +0.29R / -0.04R per trade |
| T | Flips with the market: A's longs while BTC is above its 50-day average, S's shorts (and no new longs) while below. 2.5% risk/trade. | $5,000 | +529R vs +393R for A over 8 yrs, worst year -22R vs -47R (with costs) |
| L | Long or cash: A's longs, but no new longs while BTC is below its 50-day average. 2.5% risk/trade. | $5,000 | +0.68R/+0.72R per trade (2018-22/2023-26) vs +0.51R/+0.38R for A, 4 coins, with costs |
| C | Trend basket, volatility-sized, no stops, same 4 coins. | $5,000 | 1.14 / 1.30 |
| D | Momentum long/short: weekly, long top 3 / short bottom 3 of 10 perps by 56-day return. | $5,000 | 0.71 / 1.38 |

| X | **Claude portfolio**: 50% trend sleeve + 50% momentum long/short (shorts only if falling), 10 perps, longs halved when BTC < 50-day avg, max 25%/coin, 1.5x gross. | $10,000 | +43%/yr, Sharpe 1.38 / +45%/yr, Sharpe 1.87 |

Trades are decided on finished daily closes (00:00 UTC). Live values refresh about every 15 minutes: each run starts the next one (GitHub cron is a backup). Funding rates are logged hourly to `docs/funding_log.csv` to build history for a future carry backtest.

Costs modeled: 0.12% fee + 0.05% slippage per side, hourly funding at Coinbase's current rate.

## Layout

- `bot/` strategy code (Python standard library only)
- `docs/` state, logs and the dashboard (served by GitHub Pages)
- `.github/workflows/paper-bot.yml` schedule

## Meme scanner (`docs/meme/`, dashboard at `/meme/`)

**Research + paper only.** `bot/meme_scanner.py` runs about every 15 minutes in its own workflow (`meme-scanner.yml`). It pulls candidates from DexScreener, GeckoTerminal, CoinGecko and Coinbase, reads chatter from 4chan /biz/, Reddit, crypto news and Google News/Trends, tags narratives and upcoming catalysts (GTA VI, Halloween, midterms...), checks rug risk with RugCheck (Solana) and GoPlus (EVM), and scores each coin 0-100. Coins that clear the bar get a $100 paper ticket that is tracked for 14 days, so the scanner's hit rate shows up over time.

- `docs/meme/claude_picks.json`: hand picks to track alongside the bot's (edit this file only; the bot writes the rest)
- `docs/meme/scan.json`: full data from the latest scan
- `docs/meme/ledger.json`: paper tickets

### Phone alerts

`bot/meme_watch.py` runs between scans and checks the watchlist every minute; `bot/notify.py` pushes alerts through [ntfy](https://ntfy.sh). Alerts: breakouts (+10% in 5 min on heavy, mostly-buy volume, safety-checked coins only), top picks scoring 60+, new Coinbase meme listings, 2x/5x/10x/-50% and liquidity-pulled on Claude's picks and alerted coins, and news on picks. The ntfy topic lives only in the `NTFY_TOPIC` repo secret; without it alerts are logged on the dashboard but not pushed. Alert logs: `docs/meme/alerts.json` (scanner), `docs/meme/watch.json` (watcher).
