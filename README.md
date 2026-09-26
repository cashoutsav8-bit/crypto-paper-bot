# crypto-paper-bot

Paper-trading research bot for Coinbase nano perpetual futures. **Paper only: it never places orders and uses no API keys.** It reads Coinbase's public market data, simulates each strategy, and publishes a dashboard with GitHub Pages.

## Strategies

| | Strategy | Paper capital | Backtest (2018-22 / 2023-26 Sharpe) |
|---|---|---|---|
| A | Trend + ATR stops on BTC, ETH, SOL, XRP. Enter when close > close 20 days ago; stop 2 ATR below entry, trailing 3 ATR below the high. 5% risk per trade, 15% max risk to starting capital. | $2,000 | +0.48R / +0.32R per trade |
| C | Trend basket, volatility-sized, no stops, same 4 coins. | $5,000 | 1.14 / 1.30 |
| D | Momentum long/short: weekly, long top 3 / short bottom 3 of 10 perps by 56-day return. | $5,000 | 0.71 / 1.38 |

Trades are decided on finished daily closes (00:00 UTC). Live values refresh every 15 minutes. Funding rates are logged hourly to `docs/funding_log.csv` to build history for a future carry backtest.

Costs modeled: 0.12% fee + 0.05% slippage per side, hourly funding at Coinbase's current rate.

## Layout

- `bot/` strategy code (Python standard library only)
- `docs/` state, logs and the dashboard (served by GitHub Pages)
- `.github/workflows/paper-bot.yml` schedule
