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
import health as HL

HERE = pb.HERE
START = 5000.0
FEE, SLIP = pb.FEE_RATE, pb.SLIPPAGE

C_UNIVERSE = {"BIP-20DEC30-CDE": "BTC", "ETP-20DEC30-CDE": "ETH", "SLP-20DEC30-CDE": "SOL", "XPP-20DEC30-CDE": "XRP"}
D_UNIVERSE = dict(C_UNIVERSE, **{
    "DOP-20DEC30-CDE": "DOGE", "ADP-20DEC30-CDE": "ADA", "LCP-20DEC30-CDE": "LTC",
    "LNP-20DEC30-CDE": "LINK", "AVP-20DEC30-CDE": "AVAX", "SUP-20DEC30-CDE": "SUI"})

def d(t): return pb.day(t)

class Book:
    def __init__(self, name, start=None):
        self.start = start or START
        self.dir = os.path.join(HERE, f"strat_{name}")
        os.makedirs(self.dir, exist_ok=True)
        self.state_f = os.path.join(self.dir, "state.json")
        self.s = json.load(open(self.state_f)) if os.path.exists(self.state_f) else \
            {"cash": self.start, "pos": {}, "last_t": None, "started": None, "trades": 0}
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

# ---------------- X: Claude portfolio ----------------
X_START = 10000.0
def run_X(spec, data):
    """Claude portfolio: 50% trend sleeve + 50% momentum long/short sleeve, BTC regime filter.
    Trend sleeve (daily): every coin above its close 20 days ago, sized by 0.40/annual vol, split across the universe.
    Momentum sleeve (Mondays): long top 3 by 56-day return, short bottom 3 but only if they are also in a 20-day downtrend.
    Regime: when BTC closes below its 50-day average, all longs are cut in half (shorts kept).
    Caps: 25% of equity per coin, 1.5x gross exposure, overnight margin <= 90% of equity.
    Trades only when a position changes by more than 25% of its target (avoids churning on rounding)."""
    U = list(D_UNIVERSE.values())
    B = Book("X", X_START)
    days = sorted(set.intersection(*[set(data[c]) for c in U]))
    first = B.s["last_t"] is None
    if first:
        B.s["last_t"], B.s["started"] = days[-2], d(days[-1])
        B.s["mom_w"] = {}
    for t in [x for x in days if x > B.s["last_t"]]:
        i = days.index(t)
        if i < 56: B.s["last_t"] = t; continue
        px = {c: data[c][t]["c"] for c in U}
        B.accrue_funding(px, spec)
        eq = B.equity(px, spec)
        up = {c: px[c] > data[c][days[i - 20]]["c"] for c in U}
        vol = {c: ann_vol([data[c][days[k]]["c"] for k in range(i - 30, i + 1)]) for c in U}
        btc_ma = sum(data["BTC"][days[k]]["c"] for k in range(i - 49, i + 1)) / 50
        risk_on = bool(px["BTC"] > btc_ma)
        monday = datetime.fromtimestamp(t, timezone.utc).weekday() == 0
        if monday or first or not B.s.get("mom_w"):
            score = {c: px[c] / data[c][days[i - 56]]["c"] - 1 for c in U}
            ranked = sorted(score, key=score.get, reverse=True)
            mw = {c: 0.0 for c in U}
            for c in ranked[:3]: mw[c] = 1 / 6
            for c in ranked[-3:]:
                if not up[c]: mw[c] = -1 / 6
            B.s["mom_w"] = mw
            B.log(d(t), "-", "MOMENTUM", "long " + ", ".join(f"{c} {score[c]:+.0%}" for c in ranked[:3]) +
                  " | short " + (", ".join(f"{c} {score[c]:+.0%}" for c in ranked[-3:] if not up[c]) or "none (weak coins not in downtrend)"))
            first = False
        w = {}
        for c in U:
            tw = (min(0.40 / vol[c], 2.0) / len(U)) if up[c] else 0.0
            x = 0.5 * tw + 0.5 * B.s["mom_w"].get(c, 0.0)
            if x > 0 and not risk_on: x *= 0.5
            w[c] = max(-0.25, min(0.25, x))
        g = sum(abs(v) for v in w.values())
        if g > 1.5: w = {c: v * 1.5 / g for c, v in w.items()}
        targets = {c: int(round(eq * w[c] / (px[c] * spec[c]["contract_size"]))) for c in U}
        need = sum(abs(n) * px[c] * spec[c]["contract_size"] * spec[c]["overnight_margin"] for c, n in targets.items())
        if need > 0.9 * eq:
            k = 0.9 * eq / need
            targets = {c: int(n * k) for c, n in targets.items()}
        if B.s.get("regime") != risk_on:
            B.log(d(t), "BTC", "REGIME", "risk-on: BTC above 50-day average" if risk_on else "risk-off: BTC below 50-day average, longs halved")
            B.s["regime"] = risk_on
        for c, n in targets.items():
            cur = B.s["pos"].get(c, {"n": 0})["n"]
            if n == cur: continue
            if n != 0 and cur != 0 and (n > 0) == (cur > 0) and abs(n - cur) <= 0.25 * abs(n): continue
            B.trade_to(d(t), c, n, px[c], spec)
        B.mark(d(t), px, spec)
        B.s["last_t"] = t
    B.save()
    return B

# ---------------- position health (display only, no trading logic) ----------------
def health_C(b, spec, data):
    days = sorted(set.intersection(*[set(data[c]) for c in C_UNIVERSE.values()]))
    live = {c: spec[c]["price"] for c in spec}
    eq = b.equity(live, spec); ed = HL.entry_dates(os.path.join(b.dir, "events.csv")); rows = []
    for c, p in b.s["pos"].items():
        size = spec[c]["contract_size"] * p["n"]
        r = HL.base_row(c, "LONG" if p["n"] > 0 else "SHORT", p["entry"], live[c], (live[c] - p["entry"]) * size,
                       live[c] * size, eq, ed.get(c), data["BTC"], live["BTC"])
        ref = data[c][days[len(days) - 20]]["c"] if len(days) >= 20 else None
        if ref:
            dist = live[c] / ref - 1
            r["trigger"] = f"20-day-ago close {ref:.6g}"
            r["state"] = "bad" if dist <= 0 else ("close" if dist < 0.03 else "ok")
            r["dist"] = (f"{dist:+.1%} above it: stays long" if dist > 0 else
                         f"{dist:+.1%} below it: sells at the next daily close if it stays there")
        else:
            r.update(trigger="-", state="ok", dist="-")
        rows.append(r)
    return rows

def health_D(b, spec, data):
    days = sorted(set.intersection(*[set(data[c]) for c in D_UNIVERSE.values()]))
    live = {c: spec[c]["price"] for c in spec}
    eq = b.equity(live, spec); ed = HL.entry_dates(os.path.join(b.dir, "events.csv")); rows = []
    ref_t = days[len(days) - 56] if len(days) >= 56 else None
    score = {c: live[c] / data[c][ref_t]["c"] - 1 for c in D_UNIVERSE.values() if ref_t in data[c]} if ref_t else {}
    rank = {c: k + 1 for k, c in enumerate(sorted(score, key=score.get, reverse=True))}
    wd = datetime.now(timezone.utc).weekday(); to_mon = (7 - wd) % 7 or 7
    for c, p in b.s["pos"].items():
        size = spec[c]["contract_size"] * p["n"]
        side = "LONG" if p["n"] > 0 else "SHORT"
        r = HL.base_row(c, side, p["entry"], live[c], (live[c] - p["entry"]) * size, live[c] * size, eq, ed.get(c), data["BTC"], live["BTC"])
        k = rank.get(c); n = len(rank)
        keep = (k is not None) and ((side == "LONG" and k <= 3) or (side == "SHORT" and k > n - 3))
        near = (k is not None) and ((side == "LONG" and k <= 5) or (side == "SHORT" and k > n - 5))
        r["trigger"] = f"Monday rebalance in {to_mon}d"
        r["state"] = "ok" if keep else ("close" if near else "bad")
        r["dist"] = (f"56-day rank now #{k} of {n}: " + ("would stay " + side.lower() if keep else "would be closed if Monday looked like today")) if k else "-"
        rows.append(r)
    return rows

def health_X(b, spec, data):
    days = sorted(set.intersection(*[set(data[c]) for c in D_UNIVERSE.values()]))
    live = {c: spec[c]["price"] for c in spec}
    eq = b.equity(live, spec); ed = HL.entry_dates(os.path.join(b.dir, "events.csv")); rows = []
    ref = days[len(days) - 20] if len(days) >= 20 else None
    for c, p in b.s["pos"].items():
        size = spec[c]["contract_size"] * p["n"]; side = "LONG" if p["n"] > 0 else "SHORT"
        r = HL.base_row(c, side, p["entry"], live[c], (live[c] - p["entry"]) * size, live[c] * size, eq, ed.get(c), data["BTC"], live["BTC"])
        if ref and ref in data[c]:
            lvl = data[c][ref]["c"]; up = live[c] > lvl; dist = live[c] / lvl - 1
            ok = up if side == "LONG" else not up
            r["trigger"] = f"20-day level {lvl:.6g}"
            r["state"] = "ok" if ok and abs(dist) > 0.03 else ("close" if ok else "bad")
            r["dist"] = f"{dist:+.1%} vs 20-day level: " + ("trend agrees" if ok else "trend disagrees; trims at the next daily close")
        rows.append(r)
    return rows

def health_A(spec, data):
    s = pb.load_state(); live = {c: spec[c]["price"] for c in spec}
    eq = s["cash_equity"] + pb.unrealized(s, live, spec); rows = []
    for c, p in s["positions"].items():
        size = spec[c]["contract_size"] * p["contracts"]
        r = HL.base_row(c, "LONG", p["entry"], live[c], (live[c] - p["entry"]) * size, live[c] * size, eq, p.get("entry_date"), data["BTC"], live["BTC"])
        dist = live[c] / p["stop"] - 1
        r["trigger"] = f"stop {p['stop']:.6g}"
        r["state"] = "bad" if dist <= 0 else ("close" if dist < 0.03 else "ok")
        r["dist"] = f"{dist:+.1%} above stop" if dist > 0 else "at/below stop: exits on the daily bar"
        rows.append(r)
    return rows

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

NAV_CSS = HL.CSS + ".nav{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 18px}.nav a{color:#eef3f1;text-decoration:none;border:1px solid #2a312e;border-radius:6px;padding:5px 10px;font-size:13px}.nav a.on{background:#2a312e}"
def nav(active, prefix=""):
    items = [("compare", "Comparison", "compare.html"), ("A", "A: Trend + stops", "report.html"),
             ("A2", "A2: Take half +4%", "strat_A2/report.html"),
             ("S", "S: Bear short", "strat_S/report.html"), ("T", "T: Both ways", "strat_T/report.html"),
             ("C", "C: Trend basket", "strat_C/report.html"), ("D", "D: Momentum L/S", "strat_D/report.html"),
             ("X", "X: Claude portfolio", "strat_X/report.html")]
    return HL.xnav() + "<div class=nav>" + "".join(f'<a class="{"on" if k==active else ""}" href="{prefix}{h}">{t}</a>' for k, t, h in items) + "</div>"

STRAT_INFO = {
    "C": ("C: Trend basket, vol-sized, no stops",
          "BTC, ETH, SOL, XRP. Long a coin while its close is above its close 20 days ago, flat otherwise. "
          "Each coin sized to equal risk: notional = equity x min(0.40 / annual volatility, 2) / 4, rebalanced daily in whole contracts. "
          "No stop-losses: exits happen when the 20-day trend turns down."),
    "X": ("X: Claude portfolio",
          "My best design from the research, fixed in advance so results can be judged. 10 Coinbase perps. Half the money in a trend sleeve "
          "(hold every coin above its close 20 days ago, sized by volatility), half in a momentum sleeve (every Monday: long the 3 strongest coins "
          "by 56-day return, short the 3 weakest only if they are also falling). When BTC is below its 50-day average, longs are cut in half. "
          "Max 25% per coin, 1.5x total exposure. Backtest: +43%/yr Sharpe 1.38 (2018-22), +45%/yr Sharpe 1.87 (2023-26), worst drop -32% / -15%."),
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
    _, ret, dd = stats(pts, b.start)
    gross = sum(abs(px[c] * spec[c]["contract_size"] * p["n"]) for c, p in b.s["pos"].items())
    longs = sum(1 for p in b.s["pos"].values() if p["n"] > 0); shorts = len(b.s["pos"]) - longs
    vals = [v for _, v in pts] or [b.start]
    lo, hi = min(vals + [b.start]) * 0.98, max(vals + [b.start]) * 1.02
    W, H = 640, 160
    xy = [(i * W / max(1, len(vals) - 1), H - (v - lo) / (hi - lo) * H) for i, v in enumerate(vals)]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    base_y = H - (b.start - lo) / (hi - lo) * H
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
<p class=muted>{desc}<br>PAPER ONLY, no real orders. Started {b.s['started']} with ${b.start:,.0f}. Data through {d(last)} (UTC close).</p>
<div class=tiles>
<div class=tile><b style="color:{'#5cc98a' if ret>=0 else '#ef7a79'}">${eq:,.2f}</b><span class=muted>equity ({ret:+.1%})</span></div>
<div class=tile><b style="color:{'#5cc98a' if live_eq>=b.start else '#ef7a79'}">${live_eq:,.2f}</b><span class=muted>live value now ({live_eq/b.start-1:+.1%}) at {datetime.now().strftime('%b %d %I:%M %p')}</span></div>
<div class=tile><b>{dd:.1%}</b><span class=muted>max drawdown</span></div>
<div class=tile><b>{longs} / {shorts}</b><span class=muted>long / short positions</span></div>
<div class=tile><b>${gross:,.0f}</b><span class=muted>gross exposure</span></div>
<div class=tile><b>{b.s['trades']}</b><span class=muted>rebalance trades</span></div>
</div>
<h2>Equity (daily closes)</h2>
<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto"><line x1=0 x2={W} y1={base_y:.1f} y2={base_y:.1f} stroke="#55605b" stroke-dasharray="4 4"/>
<path d="{path}" fill=none stroke="#3987e5" stroke-width=2 /></svg>
<h2>Open positions</h2><div class=wrap><table><tr><th>Coin</th><th>Side</th><th class=n>Contracts</th><th class=n>Entry</th><th class=n>Last close</th><th class=n>Live</th><th class=n>Size</th><th class=n>Unrealized $ (live)</th></tr>{rows or '<tr><td colspan=8>No open positions</td></tr>'}</table></div>
<h2>Position health</h2>{HL.table(health_C(b, spec, data) if key == "C" else (health_X(b, spec, data) if key == "X" else health_D(b, spec, data)), HL.BLURB[key])}
<h2>Recent events</h2><div class=wrap><table><tr><th>Date</th><th>Coin</th><th>Event</th><th>Detail</th></tr>{ev_rows}</table></div>
<p class=muted style="margin-top:24px">Funding uses the current hourly rate at each run (longs pay, shorts receive when positive). Fees {FEE:.2%} + slippage {SLIP:.2%} per side.</p>"""
    open(os.path.join(b.dir, "report.html"), "w", encoding="utf-8").write(html)

def compare(bC, bD, spec, data, bX=None):
    A = series(pb.EQUITY_F)
    C = series(os.path.join(bC.dir, "equity.csv"))
    D = series(os.path.join(bD.dir, "equity.csv"))
    nA = len(list(csv.DictReader(open(pb.TRADES_F)))) if os.path.exists(pb.TRADES_F) else 0
    live = {c: spec[c]["price"] for c in spec}
    sA = pb.load_state()
    liveA = sA["cash_equity"] + pb.unrealized(sA, live, spec)
    lives = {"A": liveA, "C": bC.equity(live, spec), "D": bD.equity(live, spec)}
    a2rows = []
    for key, desc in [("A2", "Same as A, but sell half at +4% and move the stop on the rest to break-even"),
                      ("S", "Bear-market short: short downtrending coins only while BTC is below its 50-day average (2.5% risk/trade)"),
                      ("T", "Trend both ways: long uptrends, short downtrends when BTC is below its 50-day average (2.5% risk/trade)")]:
        sdir = os.path.join(HERE, f"strat_{key}")
        if not os.path.exists(os.path.join(sdir, "state.json")): continue
        st = json.load(open(os.path.join(sdir, "state.json")))
        lives[key] = st["cash_equity"] + pb.unrealized(st, live, spec)
        ntr = len(list(csv.DictReader(open(os.path.join(sdir, "trades.csv"))))) if os.path.exists(os.path.join(sdir, "trades.csv")) else 0
        a2rows.append((key, desc, series(os.path.join(sdir, "equity.csv")), 5000.0, f"{ntr} closed trades"))
    rows = [("A", "Trend + ATR stops (BTC, ETH, SOL, XRP), 5% risk/trade", A, pb.START_EQUITY, f"{nA} closed trades"),
            ("C", "Trend basket, vol-sized, no stops (BTC, ETH, SOL, XRP)", C, START, f"{bC.s['trades']} rebalance trades"),
            ("D", "Momentum long/short, top 3 vs bottom 3 of 10 perps, weekly", D, START, f"{bD.s['trades']} rebalance trades")]
    rows = rows[:1] + a2rows + rows[1:]
    if bX is not None:
        lives["X"] = bX.equity(live, spec)
        rows.append(("X", "Claude portfolio: trend + momentum L/S + BTC regime filter, 10 perps", series(os.path.join(bX.dir, "equity.csv")), bX.start, f"{bX.s['trades']} rebalance trades"))
    colors = {"A": "#3987e5", "A2": "#9085e9", "S": "#e34948", "T": "#e87ba4", "C": "#1baf7a", "D": "#eb6834", "X": "#eda100", "BTC": "#c3c2b7", "MIX": "#8a938f"}
    # ---- buy-and-hold benchmarks, measured from strategy A's first day ----
    day_bar = {c: {pb.day(t): b for t, b in data[c].items()} for c in data}
    base_day = A[0][0] if A else None
    bench_rows = []
    if base_day and all(base_day in day_bar[c] for c in C_UNIVERSE.values()):
        base = {c: day_bar[c][base_day]["c"] for c in C_UNIVERSE.values()}
        common = sorted(dt for dt in day_bar["BTC"] if dt >= base_day and all(dt in day_bar[c] for c in base))
        btc = [(dt, 1 + day_bar["BTC"][dt]["c"] / base["BTC"] - 1) for dt in common]
        mix = [(dt, sum(day_bar[c][dt]["c"] / base[c] for c in base) / len(base)) for dt in common]
        lives["BTC"] = live["BTC"] / base["BTC"]
        lives["MIX"] = sum(live[c] / base[c] for c in base) / len(base)
        bench_rows = [("BTC", "Benchmark: just hold BTC", btc, 1.0, "no trades"),
                      ("MIX", "Benchmark: just hold BTC, ETH, SOL, XRP equally", mix, 1.0, "no trades")]
    rows = rows + bench_rows
    W, H = 640, 180
    allp = [v / st - 1 for _, pts, st in [(r[0], r[2], r[3]) for r in rows] for _, v in pts] + [0]
    lo, hi = min(allp) - 0.01, max(allp) + 0.01
    dates = sorted({dt for r in rows for dt, _ in r[2]})
    xi = {dt: k for k, dt in enumerate(dates)}
    paths = ""
    for key, _, pts, st, _ in rows:
        if len(pts) < 1: continue
        xy = [(xi[dt] * W / max(1, len(dates) - 1), H - ((v / st - 1) - lo) / (hi - lo) * H) for dt, v in pts]
        dash = ' stroke-dasharray="5 4"' if key in ("BTC", "MIX") else ""
        paths += f'<path d="M{" L".join(f"{x:.1f},{y:.1f}" for x, y in xy)}" fill=none stroke="{colors[key]}" stroke-width=2{dash} />'
    zero_y = H - (0 - lo) / (hi - lo) * H
    trs = ""
    for key, desc, pts, st, act in rows:
        eq, ret, dd = stats(pts, st)
        if key in ("BTC", "MIX"):
            trs += (f"<tr class=bench><td><b style='color:{colors[key]}'>—</b></td><td>{desc}</td><td class=n>since {base_day}</td>"
                    f"<td class=n>–</td><td class=n style='color:{'#5cc98a' if ret>=0 else '#ef7a79'}'>{ret:+.2%}</td>"
                    f"<td class=n>{dd:.2%}</td><td class=n>{lives[key]-1:+.2%}</td><td>{act}</td></tr>")
            continue
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
th{{color:#86918c;font-size:12px;text-transform:uppercase}} .n{{text-align:right;font-family:ui-monospace,monospace;white-space:nowrap}} .wrap{{overflow-x:auto}} tr.bench td{{color:#9aa6a0}} .legend{{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:#9aa6a0}} .legend i{{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}}
{NAV_CSS}</style>
{nav('compare')}
<h1>Strategy Comparison</h1>
<p class=muted>PAPER ONLY. Returns in % so different starting balances compare fairly. Equity and trades use daily closes (like the backtest); 'Live value now' uses current Coinbase prices and refreshes every hour. Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}.
Judge on the 8-year backtest first; a few weeks of paper results is mostly luck. The grey benchmark rows show what simply holding the coins would have done over the same days.</p>
<div class=wrap><table><tr><th></th><th>Strategy</th><th class=n>Start</th><th class=n>Equity</th><th class=n>Return</th><th class=n>Max drawdown</th><th class=n>Live value now</th><th>Activity</th></tr>{trs}</table></div>
<h2>Return since start</h2>
<div class=legend>{"".join(f'<span><i style="background:{colors[k]}"></i>{lab}</span>' for k, lab in [("A","A"),("A2","A2"),("S","S"),("T","T"),("C","C"),("D","D"),("X","X: Claude"),("BTC","Hold BTC (dashed)"),("MIX","Hold 4-coin mix (dashed)")])}</div>
<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto"><line x1=0 x2={W} y1={zero_y:.1f} y2={zero_y:.1f} stroke="#55605b" stroke-dasharray="4 4"/>{paths}</svg>
<h2>A: position health</h2>{HL.table(health_A(spec, data), HL.BLURB["A"])}
<h2>C: position health</h2>{HL.table(health_C(bC, spec, data), HL.BLURB["C"])}
<h2>D: position health</h2>{HL.table(health_D(bD, spec, data), HL.BLURB["D"])}
<p class=muted>Strategy A details: report.html. Funding history is logged hourly to funding_log.csv for a future carry backtest.</p>"""
    open(os.path.join(HERE, "compare.html"), "w", encoding="utf-8").write(html)

def main():
    print(f"Multi-strategy paper run {datetime.now().strftime('%Y-%m-%d %H:%M')} (PAPER ONLY)")
    spec, data = load(D_UNIVERSE)
    bC = run_C(spec, data); bD = run_D(spec, data); bX = run_X(spec, data)
    for name, b in [("C", bC), ("D", bD), ("X", bX)]:
        last = max(data["BTC"])
        px = {c: data[c][last]["c"] for c in data}
        print(f"  {name}: equity ${b.equity(px, spec):,.2f}, positions " +
              (", ".join(f"{c} {p['n']:+d}" for c, p in sorted(b.s['pos'].items())) or "none"))
    strat_report(bC, 'C', spec, data); strat_report(bD, 'D', spec, data); strat_report(bX, 'X', spec, data)
    compare(bC, bD, spec, data, bX)
    print("Comparison: " + os.path.join(HERE, "compare.html"))

if __name__ == "__main__":
    try: main()
    except Exception as e:
        print("ERROR:", e); raise SystemExit(1)
