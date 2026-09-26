"""
Extra paper strategies, run alongside paper_bot.py (strategy A). PAPER ONLY, no orders.

  C  Trend basket, volatility-sized, no stops (BTC, ETH, SOL, XRP)
     Long a coin while its close > close 20 days ago; flat otherwise.
     Size each coin so it carries equal risk: notional = equity x min(0.40 / annual vol, 2) / 4.
     Rebalanced daily in whole contracts. Backtest Sharpe 1.14 (2018-22), 1.30 (2023-26).

  D  Cross-coin momentum, long/short, market-neutral (10 Coinbase perps)
     Every Monday: rank coins by 56-day return, long the top 3 and short the bottom 3,
     each leg = 1/6 of equity (1x gross). Held until the next Monday.
     Backtest Sharpe 0.71 (2018-22), 1.38 (2023-26). Near-zero correlation with trend.

Both start with $5,000 of paper money: at $2,000 the contract sizes are too lumpy to
follow the sizing rules (one BTC contract is ~$840). Compare them with A in % terms.

Writes strat_C/ and strat_D/ (state, equity, events) and compare.html.
"""
import csv, json, math, os
from datetime import datetime, timezone
import paper_bot as pb

HERE = pb.HERE
START = 5000.0
FEE, SLIP = pb.FEE_RATE, pb.SLIPPAGE

C_UNIVERSE = {"BIP-20DEC30-CDE": "BTC", "ETP-20DEC30-CDE": "ETH", "SLP-20DEC30-CDE": "SOL", "XPP-20DEC30-CDE": "XRP"}
D_UNIVERSE = dict(C_UNIVERSE, **{
    "DOP-20DEC30-CDE": "DOGE", "ADP-20DEC30-CDE": "ADA", "LCP-20DEC30-CDE": "LTC",
    "LNP-20DEC30-CDE": "LINK", "AVP-20DEC30-CDE": "AVAX", "SUP-20DEC30-CDE": "SUI"})

def d(t): return pb.day(t)

class Book:
    def __init__(self, name):
        self.dir = os.path.join(HERE, f"strat_{name}")
        os.makedirs(self.dir, exist_ok=True)
        self.state_f = os.path.join(self.dir, "state.json")
        self.s = json.load(open(self.state_f)) if os.path.exists(self.state_f) else \
            {"cash": START, "pos": {}, "last_t": None, "started": None, "trades": 0}
    def save(self): json.dump(self.s, open(self.state_f, "w"), indent=2)
    def log(self, day, coin, ev, detail):
        pb.append_csv(os.path.join(self.dir, "events.csv"), [day, coin, ev, detail], ["date", "coin", "event", "detail"])
    def equity(self, px, spec):
        return self.s["cash"] + sum((px[c] - p["entry"]) * spec[c]["contract_size"] * p["n"] for c, p in self.s["pos"].items())
    def trade_to(self, day, coin, target, price, spec):
        """Move a coin to `target` contracts (negative = short). Cash holds realized P&L and costs."""
        cur = self.s["pos"].get(coin)
        n0 = cur["n"] if cur else 0
        if target == n0: return
        size = spec[coin]["contract_size"]
        fill = price * (1 + SLIP) if target > n0 else price * (1 - SLIP)
        self.s["cash"] -= abs(target - n0) * size * fill * FEE
        if n0 == 0:
            self.s["pos"][coin] = {"n": target, "entry": fill}
        elif target == 0 or (target > 0) != (n0 > 0):          # close fully (and maybe flip)
            self.s["cash"] += (fill - cur["entry"]) * size * n0
            if target == 0: self.s["pos"].pop(coin)
            else: self.s["pos"][coin] = {"n": target, "entry": fill}
        elif abs(target) < abs(n0):                              # partial close
            self.s["cash"] += (fill - cur["entry"]) * size * (n0 - target)
            cur["n"] = target
        else:                                                    # add to position
            cur["entry"] = (cur["entry"] * n0 + fill * (target - n0)) / target
            cur["n"] = target
        self.s["trades"] += 1
        self.log(day, coin, "TRADE", f"{n0:+d} -> {target:+d} contracts @ {fill:.6g}")
    def accrue_funding(self, px, spec):
        for c, p in self.s["pos"].items():
            notional = px[c] * spec[c]["contract_size"] * p["n"]      # negative for shorts
            self.s["cash"] -= notional * spec[c]["funding_hourly"] * 24
    def mark(self, day, px, spec):
        eq = self.equity(px, spec)
        gross = sum(abs(px[c] * spec[c]["contract_size"] * p["n"]) for c, p in self.s["pos"].items())
        pb.append_csv(os.path.join(self.dir, "equity.csv"), [day, round(eq, 2), len(self.s["pos"]), round(gross, 2)],
                      ["date", "equity", "positions", "gross_notional"])
        return eq

def load(universe):
    spec, data = {}, {}
    for pid, coin in universe.items():
        spec[coin] = pb.fetch_product(pid)
        bars = pb.fetch_daily(pid)
        data[coin] = {b["t"]: b for b in bars}
    return spec, data

def ann_vol(closes):
    r = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(365)

def run_C(spec, data):
    B = Book("C")
    days = sorted(set.intersection(*[set(data[c]) for c in C_UNIVERSE.values()]))
    if B.s["last_t"] is None:
        B.s["last_t"], B.s["started"] = days[-2], d(days[-1])
    for t in [x for x in days if x > B.s["last_t"]]:
        i = days.index(t)
        px = {c: data[c][t]["c"] for c in C_UNIVERSE.values()}
        B.accrue_funding(px, spec)
        eq = B.equity(px, spec)
        targets = {}
        for c in C_UNIVERSE.values():
            if i < 31: targets[c] = B.s["pos"].get(c, {"n": 0})["n"]; continue
            up = px[c] > data[c][days[i - 20]]["c"]
            vol = ann_vol([data[c][days[k]]["c"] for k in range(i - 30, i + 1)])
            w = min(0.40 / vol, 2.0) / len(C_UNIVERSE) if up else 0.0
            targets[c] = round(eq * w / (px[c] * spec[c]["contract_size"]))
        # margin check: scale down if overnight margin would exceed 90% of equity
        need = sum(abs(n) * px[c] * spec[c]["contract_size"] * spec[c]["overnight_margin"] for c, n in targets.items())
        if need > 0.9 * eq:
            k = 0.9 * eq / need
            targets = {c: math.floor(n * k) for c, n in targets.items()}
        for c, n in targets.items():
            B.trade_to(d(t), c, n, px[c], spec)
        B.mark(d(t), px, spec)
        B.s["last_t"] = t
    B.save()
    return B

def run_D(spec, data):
    B = Book("D")
    days = sorted(set.intersection(*[set(data[c]) for c in D_UNIVERSE.values()]))
    first = B.s["last_t"] is None
    if first:
        B.s["last_t"], B.s["started"] = days[-2], d(days[-1])
    for t in [x for x in days if x > B.s["last_t"]]:
        i = days.index(t)
        px = {c: data[c][t]["c"] for c in D_UNIVERSE.values()}
        B.accrue_funding(px, spec)
        monday = datetime.fromtimestamp(t, timezone.utc).weekday() == 0
        if (monday or first) and i >= 56:
            first = False
            eq = B.equity(px, spec)
            score = {c: px[c] / data[c][days[i - 56]]["c"] - 1 for c in D_UNIVERSE.values()}
            ranked = sorted(score, key=score.get, reverse=True)
            longs, shorts = ranked[:3], ranked[-3:]
            leg = eq / 6
            targets = {c: 0 for c in D_UNIVERSE.values()}
            for c in longs:  targets[c] = max(1, round(leg / (px[c] * spec[c]["contract_size"])))
            for c in shorts: targets[c] = -max(1, round(leg / (px[c] * spec[c]["contract_size"])))
            B.log(d(t), "-", "REBALANCE", "long " + ", ".join(f"{c} {score[c]:+.0%}" for c in longs) +
                  " | short " + ", ".join(f"{c} {score[c]:+.0%}" for c in shorts))
            for c, n in targets.items():
                B.trade_to(d(t), c, n, px[c], spec)
        B.mark(d(t), px, spec)
        B.s["last_t"] = t
    B.save()
    return B

# ---------------- comparison page ----------------
def series(path, col="equity"):
    if not os.path.exists(path): return []
    return [(r["date"], float(r[col])) for r in csv.DictReader(open(path))]

def stats(pts, start):
    if not pts: return start, 0.0, 0.0
    eq = [v for _, v in pts]; peak, dd = start, 0.0
    for v in eq:
        peak = max(peak, v); dd = min(dd, v / peak - 1)
    return eq[-1], eq[-1] / start - 1, dd

NAV_CSS = ".nav{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 18px}.nav a{color:#eef3f1;text-decoration:none;border:1px solid #2a312e;border-radius:6px;padding:5px 10px;font-size:13px}.nav a.on{background:#2a312e}"
def nav(active, prefix=""):
    items = [("compare", "Comparison", "compare.html"), ("A", "A: Trend + stops", "report.html"),
             ("C", "C: Trend basket", "strat_C/report.html"), ("D", "D: Momentum L/S", "strat_D/report.html")]
    return "<div class=nav>" + "".join(f'<a class="{"on" if k==active else ""}" href="{prefix}{h}">{t}</a>' for k, t, h in items) + "</div>"

STRAT_INFO = {
    "C": ("C: Trend basket, vol-sized, no stops",
          "BTC, ETH, SOL, XRP. Long a coin while its close is above its close 20 days ago, flat otherwise. "
          "Each coin sized to equal risk: notional = equity x min(0.40 / annual volatility, 2) / 4, rebalanced daily in whole contracts. "
          "No stop-losses: exits happen when the 20-day trend turns down."),
    "D": ("D: Momentum long/short, market-neutral",
          "10 Coinbase perps (BTC, ETH, SOL, XRP, DOGE, ADA, LTC, LINK, AVAX, SUI). Every Monday: rank by 56-day return, "
          "long the top 3 and short the bottom 3, each leg about 1/6 of equity. Held until the next Monday. No stop-losses."),
}

def strat_report(b, key, spec, data):
    last = max(data["BTC"])
    px = {c: data[c][last]["c"] for c in data if last in data[c]}
    eq = b.equity(px, spec)
    live = {c: spec[c]["price"] for c in spec}
    live_eq = b.equity(live, spec)
    pts = series(os.path.join(b.dir, "equity.csv"))
    _, ret, dd = stats(pts, START)
    gross = sum(abs(px[c] * spec[c]["contract_size"] * p["n"]) for c, p in b.s["pos"].items())
    longs = sum(1 for p in b.s["pos"].values() if p["n"] > 0); shorts = len(b.s["pos"]) - longs
    vals = [v for _, v in pts] or [START]
    lo, hi = min(vals + [START]) * 0.98, max(vals + [START]) * 1.02
    W, H = 640, 160
    xy = [(i * W / max(1, len(vals) - 1), H - (v - lo) / (hi - lo) * H) for i, v in enumerate(vals)]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    base_y = H - (START - lo) / (hi - lo) * H
    rows = ""
    for c, p in sorted(b.s["pos"].items()):
        pr = px.get(c, p["entry"]); lv = live.get(c, pr); notional = pr * spec[c]["contract_size"] * p["n"]
        pnl = (lv - p["entry"]) * spec[c]["contract_size"] * p["n"]
        rows += (f"<tr><td>{c}</td><td>{'LONG' if p['n']>0 else 'SHORT'}</td><td class=n>{abs(p['n'])}</td><td class=n>{p['entry']:.6g}</td>"
                 f"<td class=n>{pr:.6g}</td><td class=n>{lv:.6g}</td><td class=n>${abs(notional):,.0f}</td><td class=n style='color:{'#5cc98a' if pnl>=0 else '#ef7a79'}'>{pnl:+,.2f}</td></tr>")
    evf = os.path.join(b.dir, "events.csv")
    evs = list(csv.DictReader(open(evf))) if os.path.exists(evf) else []
    ev_rows = "".join(f"<tr><td>{e['date']}</td><td>{e['coin']}</td><td>{e['event']}</td><td>{e['detail']}</td></tr>" for e in list(reversed(evs))[:50])
    title, desc = STRAT_INFO[key]
    html = f"""<!doctype html><meta charset=utf-8><meta http-equiv="refresh" content="60"><title>{title}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;background:#111514;color:#eef3f1;max-width:900px;margin:0 auto;padding:24px 16px}}
h1{{font-size:26px;margin:0}} h2{{font-size:17px;margin:28px 0 8px}} .muted{{color:#9aa6a0}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:16px}}
.tile{{border-top:2px solid #eef3f1;padding-top:6px}} .tile b{{font:700 22px ui-monospace,monospace;display:block}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td,th{{padding:6px 8px;border-bottom:1px solid #2a312e;text-align:left}}
th{{color:#86918c;font-size:12px;text-transform:uppercase}} .n{{text-align:right;font-family:ui-monospace,monospace}} .wrap{{overflow-x:auto}}
{NAV_CSS}</style>
{nav(key, "../")}
<h1>{title}</h1>
<p class=muted>{desc}<br>PAPER ONLY, no real orders. Started {b.s['started']} with ${START:,.0f}. Data through {d(last)} (UTC close).</p>
<div class=tiles>
<div class=tile><b style="color:{'#5cc98a' if ret>=0 else '#ef7a79'}">${eq:,.2f}</b><span class=muted>equity ({ret:+.1%})</span></div>
<div class=tile><b style="color:{'#5cc98a' if live_eq>=START else '#ef7a79'}">${live_eq:,.2f}</b><span class=muted>live value now ({live_eq/START-1:+.1%}) at {datetime.now().strftime('%b %d %I:%M %p')}</span></div>
<div class=tile><b>{dd:.1%}</b><span class=muted>max drawdown</span></div>
<div class=tile><b>{longs} / {shorts}</b><span class=muted>long / short positions</span></div>
<div class=tile><b>${gross:,.0f}</b><span class=muted>gross exposure</span></div>
<div class=tile><b>{b.s['trades']}</b><span class=muted>rebalance trades</span></div>
</div>
<h2>Equity (daily closes)</h2>
<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto"><line x1=0 x2={W} y1={base_y:.1f} y2={base_y:.1f} stroke="#55605b" stroke-dasharray="4 4"/>
<path d="{path}" fill=none stroke="#3987e5" stroke-width=2 /></svg>
<h2>Open positions</h2><div class=wrap><table><tr><th>Coin</th><th>Side</th><th class=n>Contracts</th><th class=n>Entry</th><th class=n>Last close</th><th class=n>Live</th><th class=n>Size</th><th class=n>Unrealized $ (live)</th></tr>{rows or '<tr><td colspan=8>No open positions</td></tr>'}</table></div>
<h2>Recent events</h2><div class=wrap><table><tr><th>Date</th><th>Coin</th><th>Event</th><th>Detail</th></tr>{ev_rows}</table></div>
<p class=muted style="margin-top:24px">Funding uses the current hourly rate at each run (longs pay, shorts receive when positive). Fees {FEE:.2%} + slippage {SLIP:.2%} per side.</p>"""
    open(os.path.join(b.dir, "report.html"), "w", encoding="utf-8").write(html)

def compare(bC, bD, spec, data):
    A = series(pb.EQUITY_F)
    C = series(os.path.join(bC.dir, "equity.csv"))
    D = series(os.path.join(bD.dir, "equity.csv"))
    nA = len(list(csv.DictReader(open(pb.TRADES_F)))) if os.path.exists(pb.TRADES_F) else 0
    live = {c: spec[c]["price"] for c in spec}
    sA = pb.load_state()
    liveA = sA["cash_equity"] + pb.unrealized(sA, live, spec)
    lives = {"A": liveA, "C": bC.equity(live, spec), "D": bD.equity(live, spec)}
    rows = [("A", "Trend + ATR stops (BTC, ETH, SOL, XRP), 5% risk/trade", A, pb.START_EQUITY, f"{nA} closed trades"),
            ("C", "Trend basket, vol-sized, no stops (BTC, ETH, SOL, XRP)", C, START, f"{bC.s['trades']} rebalance trades"),
            ("D", "Momentum long/short, top 3 vs bottom 3 of 10 perps, weekly", D, START, f"{bD.s['trades']} rebalance trades")]
    colors = {"A": "#3987e5", "C": "#1baf7a", "D": "#eb6834"}
    W, H = 640, 180
    allp = [v / st - 1 for _, pts, st in [(r[0], r[2], r[3]) for r in rows] for _, v in pts] + [0]
    lo, hi = min(allp) - 0.01, max(allp) + 0.01
    dates = sorted({dt for r in rows for dt, _ in r[2]})
    xi = {dt: k for k, dt in enumerate(dates)}
    paths = ""
    for key, _, pts, st, _ in rows:
        if len(pts) < 1: continue
        xy = [(xi[dt] * W / max(1, len(dates) - 1), H - ((v / st - 1) - lo) / (hi - lo) * H) for dt, v in pts]
        paths += f'<path d="M{" L".join(f"{x:.1f},{y:.1f}" for x, y in xy)}" fill=none stroke="{colors[key]}" stroke-width=2 />'
    zero_y = H - (0 - lo) / (hi - lo) * H
    trs = ""
    for key, desc, pts, st, act in rows:
        eq, ret, dd = stats(pts, st)
        trs += (f"<tr><td><b style='color:{colors[key]}'>{key}</b></td><td>{desc}</td><td class=n>${st:,.0f}</td>"
                f"<td class=n>${eq:,.2f}</td><td class=n style='color:{'#5cc98a' if ret>=0 else '#ef7a79'}'>{ret:+.2%}</td>"
                f"<td class=n>{dd:.2%}</td><td class=n>${lives[key]:,.2f} ({lives[key]/st-1:+.2%})</td><td>{act}</td></tr>")
    def pos_table(b, name):
        last = max(data[next(iter(data))])
        rr = ""
        for c, p in sorted(b.s["pos"].items()):
            px = data[c][last]["c"] if last in data[c] else p["entry"]
            pnl = (px - p["entry"]) * spec[c]["contract_size"] * p["n"]
            rr += f"<tr><td>{c}</td><td>{'LONG' if p['n']>0 else 'SHORT'}</td><td class=n>{abs(p['n'])}</td><td class=n>{p['entry']:.6g}</td><td class=n>{px:.6g}</td><td class=n>{pnl:+,.2f}</td></tr>"
        return f"<h2>{name} positions</h2><div class=wrap><table><tr><th>Coin</th><th>Side</th><th class=n>Contracts</th><th class=n>Entry</th><th class=n>Last</th><th class=n>Unrealized $</th></tr>{rr or '<tr><td colspan=6>None</td></tr>'}</table></div>"
    html = f"""<!doctype html><meta charset=utf-8><meta http-equiv="refresh" content="60"><title>Strategy Comparison</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;background:#111514;color:#eef3f1;max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:26px;margin:0}} h2{{font-size:17px;margin:28px 0 8px}} .muted{{color:#9aa6a0}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td,th{{padding:6px 8px;border-bottom:1px solid #2a312e;text-align:left;vertical-align:top}}
th{{color:#86918c;font-size:12px;text-transform:uppercase}} .n{{text-align:right;font-family:ui-monospace,monospace;white-space:nowrap}} .wrap{{overflow-x:auto}}
{NAV_CSS}</style>
{nav('compare')}
<h1>Strategy Comparison</h1>
<p class=muted>PAPER ONLY. Returns in % so different starting balances compare fairly. Equity and trades use daily closes (like the backtest); 'Live value now' uses current Coinbase prices and refreshes every hour. Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}.
Judge on the 8-year backtest first; a few weeks of paper results is mostly luck.</p>
<div class=wrap><table><tr><th></th><th>Strategy</th><th class=n>Start</th><th class=n>Equity</th><th class=n>Return</th><th class=n>Max drawdown</th><th class=n>Live value now</th><th>Activity</th></tr>{trs}</table></div>
<h2>Return since start</h2>
<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto"><line x1=0 x2={W} y1={zero_y:.1f} y2={zero_y:.1f} stroke="#55605b" stroke-dasharray="4 4"/>{paths}</svg>
{pos_table(bC, "C")}{pos_table(bD, "D")}
<p class=muted>Strategy A details: report.html. Funding history is logged hourly to funding_log.csv for a future carry backtest.</p>"""
    open(os.path.join(HERE, "compare.html"), "w", encoding="utf-8").write(html)

def main():
    print(f"Multi-strategy paper run {datetime.now().strftime('%Y-%m-%d %H:%M')} (PAPER ONLY)")
    spec, data = load(D_UNIVERSE)
    bC = run_C(spec, data); bD = run_D(spec, data)
    for name, b in [("C", bC), ("D", bD)]:
        last = max(data["BTC"])
        px = {c: data[c][last]["c"] for c in data}
        print(f"  {name}: equity ${b.equity(px, spec):,.2f}, positions " +
              (", ".join(f"{c} {p['n']:+d}" for c, p in sorted(b.s['pos'].items())) or "none"))
    strat_report(bC, 'C', spec, data); strat_report(bD, 'D', spec, data)
    compare(bC, bD, spec, data)
    print("Comparison: " + os.path.join(HERE, "compare.html"))

if __name__ == "__main__":
    try: main()
    except Exception as e:
        print("ERROR:", e); raise SystemExit(1)
