"""
Position health panel for the crypto dashboards. DISPLAY ONLY: nothing here changes any trading rule.

For each open position it answers:
  - how much has it moved since entry, and how much did BTC (the market) move over the same days?
  - is the move coin-specific or just the market?
  - what is the exit trigger for this strategy, and how far is the price from it right now?
"""
import csv, os
from datetime import datetime, timezone

CSS = (".pill{display:inline-block;font:600 11px/1 ui-monospace,monospace;padding:3px 6px;border-radius:4px;background:#2a312e}"
       ".pill.ok{background:#1c3b2c;color:#79d9a3}.pill.close{background:#3f3219;color:#f0c374}.pill.bad{background:#40211f;color:#f39a93}"
       ".hbox{border:1px solid #2a312e;border-radius:8px;padding:10px 14px;margin:8px 0 10px}.hbox p{margin:4px 0}"
       ".ht td,.ht th{white-space:nowrap}"
       ".xnav{display:flex;gap:8px;margin:0 0 10px;font-size:13px}.xnav a{color:#eef3f1;text-decoration:none;padding:4px 10px;border-radius:14px;border:1px solid #2a312e}"
       ".xnav a.on{background:#eef3f1;color:#111514;font-weight:600}")
STOCKS_URL = "https://cashoutsav8-bit.github.io/stock-paper-bot/"
CRYPTO_URL = "https://cashoutsav8-bit.github.io/crypto-paper-bot/"


def xnav():
    return f'<div class=xnav><a class=on href="{CRYPTO_URL}">Crypto bot</a><a href="{STOCKS_URL}">Stock bot</a></div>'


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def entry_dates(events_path):
    """Date each current position was opened: the last trade that started from zero contracts."""
    out = {}
    if not os.path.exists(events_path):
        return out
    for e in csv.DictReader(open(events_path, encoding="utf-8")):
        det = e.get("detail", "")
        if e.get("event") in ("TRADE", "ENTER") and (det.startswith("+0 ->") or e["event"] == "ENTER"):
            out[e["coin"]] = e["date"]
    return out


def close_on(bars, date_s):
    """Close of the daily bar for date_s (or the latest bar before it)."""
    best = None
    for t in sorted(bars):
        if day(t) <= date_s:
            best = bars[t]["c"]
    return best


def _cls(x):
    return "#5cc98a" if x >= 0 else "#ef7a79"


def table(rows, blurb):
    """rows: dicts with coin, side, entry_date, days, entry, live, move, btc, pnl, weight, trigger, state, dist"""
    if not rows:
        return "<p class=muted>No open positions.</p>"
    rows = sorted(rows, key=lambda r: r["pnl"])
    total = sum(r["pnl"] for r in rows)

    def read(r):
        if r["coin"] == "BTC":
            return "BTC is the market"
        rel = (r["move"] - r["btc"]) * (1 if r["side"] == "LONG" else -1)
        if abs(r["move"] - r["btc"]) < 0.02:
            return "moving with BTC (market)"
        return "coin-specific, helping" if rel > 0 else "coin-specific, hurting"

    def fmt(r):
        return f"<b>{r['coin']}</b> {r['side'].lower()} {r['pnl']:+,.2f} (coin {r['move']:+.1%} vs BTC {r['btc']:+.1%}: {read(r)})"
    drags = [r for r in rows if r["pnl"] < 0][:3]
    helps = [r for r in reversed(rows) if r["pnl"] > 0][:3]
    box = (f"<div class=hbox><p><b>Why the open positions are {'up' if total >= 0 else 'down'}</b> "
           f"(<span style='color:{_cls(total)}'>{total:+,.2f}</span> unrealized, live)</p>"
           + (f"<p>Biggest drags: {'; '.join(fmt(r) for r in drags)}</p>" if drags else "")
           + (f"<p>Biggest helps: {'; '.join(fmt(r) for r in helps)}</p>" if helps else "")
           + f"<p class=muted>{blurb}</p></div>")
    trs = "".join(
        f"<tr><td><b>{r['coin']}</b></td><td>{r['side']}</td><td>{r['entry_date'] or '-'}</td><td class=n>{r['days'] if r['days'] is not None else '-'}</td>"
        f"<td class=n>{r['entry']:.6g}</td><td class=n>{r['live']:.6g}</td><td class=n style='color:{_cls(r['move'])}'>{r['move']:+.1%}</td>"
        f"<td class=n>{r['btc']:+.1%}</td><td>{read(r)}</td><td class=n style='color:{_cls(r['pnl'])}'>{r['pnl']:+,.2f}</td>"
        f"<td class=n>{r['weight']:.0%}</td><td><span class='pill {r['state']}'>{r['trigger']}</span></td><td>{r['dist']}</td></tr>"
        for r in rows)
    return (box + "<div class=wrap><table class=ht><tr><th>Coin</th><th>Side</th><th>Entered</th><th class=n>Days</th><th class=n>Entry</th>"
            "<th class=n>Live</th><th class=n>Move</th><th class=n>BTC same period</th><th>Read</th><th class=n>Unrealized $</th>"
            "<th class=n>Size / equity</th><th>Exit trigger</th><th>Distance to exit</th></tr>" + trs + "</table></div>")


def base_row(coin, side, entry, live, pnl, notional, equity, entry_date, btc_bars, btc_live):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    b0 = close_on(btc_bars, entry_date) if entry_date else None
    days = (datetime.strptime(now, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days if entry_date else None
    return dict(coin=coin, side=side, entry=entry, live=live, move=live / entry - 1, btc=(btc_live / b0 - 1) if b0 else 0.0,
                pnl=pnl, weight=abs(notional) / equity if equity else 0, entry_date=entry_date, days=days)


BLURB = {
    "X": "X has no per-trade stops. Risk is controlled by position size (volatility-based, max 25% per coin, 1.5x total), "
         "by dropping a coin as soon as its 20-day trend turns, and by halving all longs when BTC falls below its 50-day average.",
    "A": "Every position has a hard stop that only moves up; a loser can cost about 5% of the account at most, and nothing needs doing by hand. "
         "A position down with BTC also down is market noise. Coin-specific weakness is exactly what the stop is for.",
    "C": "C has no stops by design. It holds each coin while today's close is above the close 20 days earlier and sells at the first daily close that isn't. "
         "Positions are sized so each coin carries the same risk, so one coin moving against you is a small slice of the account.",
    "D": "D is long the 3 strongest and short the 3 weakest coins, so a market-wide move mostly cancels out. What matters is the gap between the longs and the shorts. "
         "It only changes positions at the Monday rebalance; there are no stops.",
}
