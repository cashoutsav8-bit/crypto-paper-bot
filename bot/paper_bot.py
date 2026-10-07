"""
Paper-trading bot: trend-following with ATR stops on Coinbase nano perps.
PAPER ONLY. It never places orders and needs no API key. It reads Coinbase's
public market data, simulates the strategy, and writes logs plus report.html.

Rules (same as the backtest):
  Entry : when flat and today's close > close 20 days ago (uptrend), buy at the close.
  Stop  : initial stop = entry - 2 x ATR(14). Trail = highest high since entry - 3 x ATR.
          The stop only moves up. Exit if a day's low touches it (or the open gaps below it).
  Size  : risk RISK_PER_TRADE of current equity per trade (whole contracts).
          If 1 contract risks up to 1.5x the budget, take 1 contract.
  Caps  : total open risk <= MAX_OPEN_RISK of equity; overnight margin <= MAX_MARGIN_USE.
  Costs : fee + slippage per side, hourly funding on open positions.

Run it once a day, any time after 8:05 PM New York time (00:05 UTC).
Missed days are caught up automatically on the next run.
"""
import csv, json, math, os, sys, time, urllib.request
from datetime import datetime, timezone

# ---------------- settings ----------------
# Variant settings (A = defaults; A2 = take half at +4%, set by the workflow via env vars)
NAME             = os.environ.get("PAPERBOT_NAME", "A")
TAKE_HALF_AT     = float(os.environ.get("PAPERBOT_TP", "0") or 0)   # e.g. 0.04 = sell half at +4%, stop on rest to break-even
START_EQUITY     = float(os.environ.get("PAPERBOT_START", "2000"))
RISK_PER_TRADE   = 0.05     # 5% of equity at risk per trade
MAX_OPEN_RISK    = 0.15     # max 15% of equity at risk across all open positions
MAX_MARGIN_USE   = 0.90     # overnight margin may use at most 90% of equity
FEE_RATE         = 0.0012   # per side, ~Coinbase nano futures fee (your order form showed $0.91 on $789)
SLIPPAGE         = 0.0005   # per side
LOOKBACK         = 20       # trend lookback in days
ATR_N            = 14
STOP_ATR         = 2.0
TRAIL_ATR        = 3.0
PRODUCTS = {                # Coinbase product id : label
    "BIP-20DEC30-CDE": "BTC",
    "ETP-20DEC30-CDE": "ETH",
    "SLP-20DEC30-CDE": "SOL",
    "XPP-20DEC30-CDE": "XRP",
}
API = "https://api.coinbase.com/api/v3/brokerage/market/products/"
HERE = os.environ.get("PAPERBOT_DATA") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(HERE, exist_ok=True)
STATE_F  = os.path.join(HERE, "state.json")
TRADES_F = os.path.join(HERE, "trades.csv")
EQUITY_F = os.path.join(HERE, "equity.csv")
EVENTS_F = os.path.join(HERE, "events.csv")
REPORT_F = os.path.join(HERE, "report.html")

# ---------------- data ----------------
def get_json(url, tries=3):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if k == tries - 1:
                raise
            time.sleep(2 + 3 * k)

def fetch_product(pid):
    j = get_json(API + pid)
    f = j["future_product_details"]
    return {
        "price": float(j["price"]),
        "contract_size": float(f["contract_size"]),
        "funding_hourly": float(f.get("funding_rate") or 0.0),
        "overnight_margin": float(f["overnight_margin_rate"]["long_margin_rate"]),
    }

def fetch_daily(pid, days=300):
    end = int(time.time())
    start = end - days * 86400
    j = get_json(f"{API}{pid}/candles?granularity=ONE_DAY&start={start}&end={end}")
    today0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    bars = []
    for c in j.get("candles", []):
        t = int(c["start"])
        if t >= today0:          # drop today's unfinished candle
            continue
        bars.append({"t": t, "o": float(c["open"]), "h": float(c["high"]),
                     "l": float(c["low"]), "c": float(c["close"])})
    bars.sort(key=lambda b: b["t"])
    return bars

def add_indicators(bars):
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["c"]
        b["tr"] = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
        b["atr"] = (sum(x["tr"] for x in bars[i - ATR_N + 1:i + 1]) / ATR_N) if i >= ATR_N else None
        b["up"] = (b["c"] > bars[i - LOOKBACK]["c"]) if i >= LOOKBACK else None
    return bars

def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")

# ---------------- state & logs ----------------
def load_state():
    if os.path.exists(STATE_F):
        with open(STATE_F) as f:
            return json.load(f)
    return {"cash_equity": START_EQUITY, "last_t": None, "positions": {}, "started": None}

def save_state(s):
    with open(STATE_F, "w") as f:
        json.dump(s, f, indent=2)

def append_csv(path, row, header):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)

def event(d, coin, what, detail):
    append_csv(EVENTS_F, [d, coin, what, detail], ["date", "coin", "event", "detail"])
    print(f"  {d}  {coin:4s} {what:10s} {detail}")

# ---------------- simulation ----------------
def open_risk(s, prices, specs):
    """Risk to starting capital: how far each stop sits BELOW its entry price.
    Once a stop is above entry, that position can only give back profit, so it counts 0."""
    r = 0.0
    for coin, p in s["positions"].items():
        r += max(0.0, p["entry"] - p["stop"]) * specs[coin]["contract_size"] * p["contracts"]
    return r

def open_profit_at_risk(s, prices, specs):
    """Open profit you'd give back if every stop hit from today's price."""
    return sum(max(0.0, prices[c] - max(p["stop"], p["entry"])) * specs[c]["contract_size"] * p["contracts"]
               for c, p in s["positions"].items())

def margin_used(s, prices, specs):
    return sum(prices[c] * specs[c]["contract_size"] * p["contracts"] * specs[c]["overnight_margin"]
               for c, p in s["positions"].items())

def unrealized(s, prices, specs):
    return sum((prices[c] - p["entry"]) * specs[c]["contract_size"] * p["contracts"]
               for c, p in s["positions"].items())

def main():
    print(f"Paper bot run {datetime.now().strftime('%Y-%m-%d %H:%M')} (PAPER ONLY, no orders are placed)")
    specs, data = {}, {}
    for pid, coin in PRODUCTS.items():
        specs[coin] = fetch_product(pid)
        data[coin] = {b["t"]: b for b in add_indicators(fetch_daily(pid))}
    days = sorted(set.intersection(*[set(d.keys()) for d in data.values()]))
    s = load_state()
    if s["last_t"] is None:              # first run: start trading from the latest finished day
        s["last_t"] = days[-2]
        s["started"] = day(days[-1])
        print(f"First run. Starting paper account with ${START_EQUITY:,.2f} on {s['started']}.")
    todo = [t for t in days if t > s["last_t"]]
    if not todo:
        print("Already up to date for the last finished day. Nothing new to do.")
    for t in todo:
        d = day(t)
        bars = {c: data[c][t] for c in data}
        # 1) exits and stop updates on existing positions
        exited_today = set()
        for coin in list(s["positions"].keys()):
            p, b, sp = s["positions"][coin], bars[coin], specs[coin]
            size = sp["contract_size"] * p["contracts"]
            funding = b["c"] * size * sp["funding_hourly"] * 24
            s["cash_equity"] -= funding
            p["funding_paid"] += funding
            exit_px = None
            took_half = False
            if b["o"] <= p["stop"]:
                exit_px, why = b["o"], "gap below stop"
            elif b["l"] <= p["stop"]:
                exit_px, why = p["stop"], "stop hit"
            if exit_px is not None:
                fill = exit_px * (1 - SLIPPAGE)
                fee = fill * size * FEE_RATE
                pnl = (fill - p["entry"]) * size - fee - p["entry_fee"] - p["funding_paid"] + p.get("realized", 0.0)
                s["cash_equity"] += (fill - p["entry"]) * size - fee
                R = pnl / p["risk_usd"] if p["risk_usd"] else 0
                append_csv(TRADES_F, [coin, p["entry_date"], d, p["contracts"], round(p["entry"], 6), round(fill, 6),
                                      round(pnl, 2), round(R, 2), why],
                           ["coin", "entry_date", "exit_date", "contracts", "entry", "exit", "pnl_usd", "R", "reason"])
                event(d, coin, "EXIT", f"{p['contracts']} @ {fill:.6g} ({why}), P&L ${pnl:,.2f} = {R:+.2f}R")
                del s["positions"][coin]
                exited_today.add(coin)
                continue
            # A2 only: bank half at +TAKE_HALF_AT, then the rest can't become a loss (stop to break-even)
            if TAKE_HALF_AT and not p.get("half_taken") and b["h"] >= p["entry"] * (1 + TAKE_HALF_AT):
                p["half_taken"] = True; took_half = True
                tp_px = max(b["o"], p["entry"] * (1 + TAKE_HALF_AT))
                k = p["contracts"] // 2
                if k >= 1:
                    fill = tp_px * (1 - SLIPPAGE); part = sp["contract_size"] * k
                    fee = fill * part * FEE_RATE
                    gain = (fill - p["entry"]) * part - fee
                    s["cash_equity"] += gain
                    p["realized"] = p.get("realized", 0.0) + gain
                    p["contracts"] -= k
                    event(d, coin, "TAKE HALF", f"sold {k} @ {fill:.6g} (+{TAKE_HALF_AT:.0%}), banked ${gain:,.2f}; {p['contracts']} left")
                else:
                    event(d, coin, "TAKE HALF", f"+{TAKE_HALF_AT:.0%} reached but only 1 contract; keeping it")
                if p["entry"] > p["stop"]:
                    event(d, coin, "RAISE STOP", f"{p['stop']:.6g} -> {p['entry']:.6g} (break-even)")
                    p["stop"] = p["entry"]
            p["high"] = max(p["high"], b["h"])
            new_stop = max(p["stop"], p["high"] - TRAIL_ATR * b["atr"])
            if new_stop > p["stop"] + 1e-12:
                event(d, coin, "RAISE STOP", f"{p['stop']:.6g} -> {new_stop:.6g}")
                p["stop"] = new_stop
        # 2) entries
        prices = {c: bars[c]["c"] for c in bars}
        equity = s["cash_equity"] + unrealized(s, prices, specs)
        for coin in PRODUCTS.values():
            b, sp = bars[coin], specs[coin]
            if coin in s["positions"] or coin in exited_today or s.get("exited_on", {}).get(coin) == d \
                    or not b["up"] or b["atr"] is None:
                continue
            stop = b["c"] - STOP_ATR * b["atr"]
            risk_1 = (b["c"] - stop) * sp["contract_size"]
            budget = RISK_PER_TRADE * equity
            n = math.floor(budget / risk_1)
            if n == 0 and risk_1 <= 1.5 * budget:
                n = 1
            if n == 0:
                event(d, coin, "SKIP", f"1 contract risks ${risk_1:,.0f}, budget ${budget:,.0f}")
                continue
            room = MAX_OPEN_RISK * equity - open_risk(s, prices, specs)
            while n > 0 and n * risk_1 > room:
                n -= 1
            if n == 0:
                event(d, coin, "SKIP", f"open-risk cap {MAX_OPEN_RISK:.0%} reached")
                continue
            m_room = MAX_MARGIN_USE * equity - margin_used(s, prices, specs)
            m_1 = b["c"] * sp["contract_size"] * sp["overnight_margin"]
            while n > 0 and n * m_1 > m_room:
                n -= 1
            if n == 0:
                event(d, coin, "SKIP", f"not enough margin (needs ${m_1:,.0f}/contract overnight)")
                continue
            fill = b["c"] * (1 + SLIPPAGE)
            size = sp["contract_size"] * n
            fee = fill * size * FEE_RATE
            s["cash_equity"] -= fee
            s["positions"][coin] = {"entry": fill, "entry_date": d, "contracts": n, "stop": stop,
                                    "high": b["c"], "risk_usd": (fill - stop) * size + 2 * fee,
                                    "entry_fee": fee, "funding_paid": 0.0}
            event(d, coin, "ENTER", f"{n} contract(s) @ {fill:.6g}, stop {stop:.6g}, "
                                     f"risk ${(fill - stop) * size:,.0f} ({(fill - stop) * size / equity:.1%})")
        equity = s["cash_equity"] + unrealized(s, prices, specs)
        append_csv(EQUITY_F, [d, round(equity, 2), len(s["positions"]),
                              round(open_risk(s, prices, specs), 2), round(margin_used(s, prices, specs), 2)],
                   ["date", "equity", "open_positions", "open_risk_usd", "overnight_margin_usd"])
        s["last_t"] = t
    # 3) intraday stops: every run (~15 min), exit any position whose live price is at or below its stop,
    #    the way a resting stop order on the exchange would. Booked at the stop price (same as the backtest).
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for coin in list(s["positions"].keys()):
        p, sp = s["positions"][coin], specs[coin]
        if sp["price"] > p["stop"]:
            continue
        size = sp["contract_size"] * p["contracts"]
        fill = p["stop"] * (1 - SLIPPAGE)
        fee = fill * size * FEE_RATE
        pnl = (fill - p["entry"]) * size - fee - p["entry_fee"] - p["funding_paid"] + p.get("realized", 0.0)
        s["cash_equity"] += (fill - p["entry"]) * size - fee
        R = pnl / p["risk_usd"] if p["risk_usd"] else 0
        append_csv(TRADES_F, [coin, p["entry_date"], today, p["contracts"], round(p["entry"], 6), round(fill, 6),
                              round(pnl, 2), round(R, 2), "stop hit (intraday)"],
                   ["coin", "entry_date", "exit_date", "contracts", "entry", "exit", "pnl_usd", "R", "reason"])
        event(today, coin, "EXIT", f"{p['contracts']} @ {fill:.6g} (stop hit intraday, live {sp['price']:.6g}), P&L ${pnl:,.2f} = {R:+.2f}R")
        del s["positions"][coin]
        s.setdefault("exited_on", {})[coin] = today   # no re-entry until the next daily close
    save_state(s)
    write_report(s, specs, data, days[-1])

# ---------------- report ----------------
PFX = "../" if NAME != "A" else ""
TITLE = "A: Trend + ATR stops" if NAME == "A" else f"{NAME}: Trend + stops, take half at +{TAKE_HALF_AT:.0%}"
DESC = ("Trend + ATR stops on Coinbase nano perps (BTC, ETH, SOL, XRP)." if NAME == "A" else
        f"Same entries and stops as A. When a trade is up {TAKE_HALF_AT:.0%}, sell half and move the stop on the rest to break-even; the rest keeps trailing.")
def nav_html():
    items = [("compare", "Comparison", "compare.html"), ("A", "A: Trend + stops", "report.html"),
             ("A2", "A2: Take half +4%", "strat_A2/report.html"),
             ("C", "C: Trend basket", "strat_C/report.html"), ("D", "D: Momentum L/S", "strat_D/report.html"),
             ("X", "X: Claude portfolio", "strat_X/report.html")]
    return "<div class=nav>" + "".join(f'<a class="{"on" if k == NAME else ""}" href="{PFX}{h}">{t}</a>' for k, t, h in items) + "</div>"

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))

def write_report(s, specs, data, last_t):
    prices = {c: data[c][last_t]["c"] for c in data}
    live = {c: specs[c]["price"] for c in specs}
    live_eq = s["cash_equity"] + unrealized(s, live, specs)
    now_s = datetime.now().strftime("%b %d %I:%M %p")
    warn = "".join(f"<p style='color:#ef7a79'><b>{c}: live price {live[c]:.6g} is below its stop {p['stop']:.6g}.</b> The exit is booked at the next daily close run (the backtest also exits on the daily low).</p>"
                   for c, p in s["positions"].items() if live[c] <= p["stop"])
    eq_rows, trades, events = read_csv(EQUITY_F), read_csv(TRADES_F), read_csv(EVENTS_F)
    equity = s["cash_equity"] + unrealized(s, prices, specs)
    ret = equity / START_EQUITY - 1
    wins = [t for t in trades if float(t["pnl_usd"]) > 0]
    pos_rows = "".join(
        f"<tr><td>{c}</td><td>{p['entry_date']}</td><td class=n>{p['contracts']}</td><td class=n>{p['entry']:.6g}</td>"
        f"<td class=n>{prices[c]:.6g}</td><td class=n>{live[c]:.6g}</td><td class=n><b>{p['stop']:.6g}</b></td>"
        f"<td class=n>{(live[c]-p['entry'])*specs[c]['contract_size']*p['contracts']:+,.2f}</td></tr>"
        for c, p in s["positions"].items()) or "<tr><td colspan=8>No open positions</td></tr>"
    tr_rows = "".join(
        f"<tr><td>{t['coin']}</td><td>{t['entry_date']}</td><td>{t['exit_date']}</td><td class=n>{t['contracts']}</td>"
        f"<td class=n>{float(t['pnl_usd']):+,.2f}</td><td class=n>{float(t['R']):+.2f}R</td><td>{t['reason']}</td></tr>"
        for t in reversed(trades)) or "<tr><td colspan=7>No closed trades yet</td></tr>"
    ev_rows = "".join(f"<tr><td>{e['date']}</td><td>{e['coin']}</td><td>{e['event']}</td><td>{e['detail']}</td></tr>"
                      for e in list(reversed(events))[:40])
    pts = [float(r["equity"]) for r in eq_rows] or [START_EQUITY]
    lo, hi = min(pts + [START_EQUITY]) * 0.98, max(pts + [START_EQUITY]) * 1.02
    W, H = 640, 160
    xy = [(i * W / max(1, len(pts) - 1), H - (v - lo) / (hi - lo) * H) for i, v in enumerate(pts)]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    base_y = H - (START_EQUITY - lo) / (hi - lo) * H
    import health as HL   # display only
    hrows = []
    for c, p in s["positions"].items():
        size = specs[c]["contract_size"] * p["contracts"]
        r = HL.base_row(c, "LONG", p["entry"], live[c], (live[c] - p["entry"]) * size, live[c] * size, live_eq,
                       p.get("entry_date"), data["BTC"], live["BTC"])
        dist = live[c] / p["stop"] - 1
        r.update(trigger=f"stop {p['stop']:.6g}", state="bad" if dist <= 0 else ("close" if dist < 0.03 else "ok"),
                 dist=f"{dist:+.1%} above stop" if dist > 0 else "at/below stop: exits on the daily bar")
        hrows.append(r)
    health_html = HL.table(hrows, HL.BLURB["A"])
    html = f"""<!doctype html><meta charset=utf-8><meta http-equiv="refresh" content="60"><title>{TITLE}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;background:#111514;color:#eef3f1;max-width:900px;margin:0 auto;padding:24px 16px}}
h1{{font-size:26px;margin:0}} h2{{font-size:17px;margin:28px 0 8px}} .muted{{color:#9aa6a0}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:16px}}
.tile{{border-top:2px solid #eef3f1;padding-top:6px}} .tile b{{font:700 22px ui-monospace,monospace;display:block}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td,th{{padding:6px 8px;border-bottom:1px solid #2a312e;text-align:left}}
th{{color:#86918c;font-size:12px;text-transform:uppercase}} .n{{text-align:right;font-family:ui-monospace,monospace}}
.wrap{{overflow-x:auto}} .pos{{color:#5cc98a}} .neg{{color:#ef7a79}}
{HL.CSS}.nav{{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 18px}}.nav a{{color:#eef3f1;text-decoration:none;border:1px solid #2a312e;border-radius:6px;padding:5px 10px;font-size:13px}}.nav a.on{{background:#2a312e}}</style>
{HL.xnav()}{nav_html()}
<h1>{TITLE}</h1>
<p class=muted>{DESC} PAPER ONLY, no real orders.
Started {s['started']} with ${START_EQUITY:,.0f}. Data through {day(last_t)} (UTC close).</p>
<div class=tiles>
<div class=tile><b class="{'pos' if ret>=0 else 'neg'}">${equity:,.2f}</b><span class=muted>equity ({ret:+.1%})</span></div>
<div class=tile><b class="{'pos' if live_eq>=START_EQUITY else 'neg'}">${live_eq:,.2f}</b><span class=muted>live value now ({live_eq/START_EQUITY-1:+.1%}) at {now_s}</span></div>
<div class=tile><b>{len(s['positions'])}</b><span class=muted>open positions</span></div>
<div class=tile><b>{len(trades)}</b><span class=muted>closed trades ({len(wins)} wins)</span></div>
<div class=tile><b>${open_risk(s, prices, specs):,.0f}</b><span class=muted>starting money at risk (stops below entry)</span></div>
<div class=tile><b>${open_profit_at_risk(s, prices, specs):,.0f}</b><span class=muted>open profit you'd give back if all stops hit</span></div>
</div>
{warn}
<h2>Equity (daily closes)</h2>
<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto"><line x1=0 x2={W} y1={base_y:.1f} y2={base_y:.1f} stroke="#55605b" stroke-dasharray="4 4"/>
<path d="{path}" fill=none stroke="#3987e5" stroke-width=2 /></svg>
<h2>Open positions and current stops</h2><div class=wrap><table><tr><th>Coin</th><th>Entered</th><th class=n>Contracts</th><th class=n>Entry</th><th class=n>Last close</th><th class=n>Live</th><th class=n>Stop</th><th class=n>Unrealized $ (live)</th></tr>{pos_rows}</table></div>
<h2>Position health</h2>{health_html}
<h2>Closed trades</h2><div class=wrap><table><tr><th>Coin</th><th>Entry</th><th>Exit</th><th class=n>Contracts</th><th class=n>P&amp;L</th><th class=n>R</th><th>Reason</th></tr>{tr_rows}</table></div>
<h2>Recent events</h2><div class=wrap><table><tr><th>Date</th><th>Coin</th><th>Event</th><th>Detail</th></tr>{ev_rows}</table></div>
<p class=muted style="margin-top:24px">Funding uses the current hourly rate at each run. Fees {FEE_RATE:.2%} + slippage {SLIPPAGE:.2%} per side.</p>"""
    with open(REPORT_F, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nEquity ${equity:,.2f} ({ret:+.1%}) | open positions {len(s['positions'])} | closed trades {len(trades)}")
    for c, p in s["positions"].items():
        print(f"  {c}: {p['contracts']} @ {p['entry']:.6g}, stop {p['stop']:.6g}, last {prices[c]:.6g}")
    print(f"Report: {REPORT_F}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
