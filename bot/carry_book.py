"""Strategy F: funding carry (cash-and-carry) on LTC and SUI. PAPER ONLY, no orders are placed.

For each coin: buy the coin outright (spot) and short the same number of coins on its Coinbase perp.
Price moves cancel out (the two legs offset); while funding is positive, the short perp collects it every hour.

Rules
  Enter a coin when its last-7-day average funding is at least 10%/yr (needs 3+ days of history).
  Exit when the last-7-day average drops below 4%/yr or the last-3-day average turns negative.
  Each active coin gets half the account: spot = alloc / 1.5, the other third stays as collateral for the short
  (well above Coinbase's short margin), so a price spike can't liquidate the short.
Costs: spot 0.40% (maker, limit order) + 0.05% slippage per side; perp 0.12% fee + 0.05% slippage per side.
Funding: the real hourly rates from funding_log.csv (written by funding_logger.py every run), applied to the
short's notional at the logged perp price. Equity is marked net of the fees it would cost to close everything now.
"""
import os, csv, json
from datetime import datetime, timezone

HERE = os.environ.get("PAPERBOT_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
LOG = os.path.join(HERE, "funding_log.csv")
DIR = os.path.join(HERE, "strat_F"); os.makedirs(DIR, exist_ok=True)
STATE_F, EQUITY_F, EVENTS_F, REPORT_F = (os.path.join(DIR, f) for f in ("state.json", "equity.csv", "events.csv", "report.html"))

START = 5000.0
COINS = {"LTC": "LCP-20DEC30-CDE", "SUI": "SUP-20DEC30-CDE"}
ENTER_APR, EXIT_APR = 0.10, 0.04
COLLATERAL = 0.5                      # perp collateral as a share of notional
SPOT_COST = 0.0040 + 0.0005           # per side
PERP_COST = 0.0012 + 0.0005           # per side
HOURS_YR = 24 * 365

def read_log():
    """{product: [(funding_time, rate, price, index_price, utc_time), ...]} one row per funding hour (latest reading)."""
    out = {}
    if not os.path.exists(LOG): return out
    for r in csv.DictReader(open(LOG)):
        try:
            rate = float(r["funding_rate_hourly"]); px = float(r["price"]); ix = float(r["index_price"] or r["price"])
        except (TypeError, ValueError):
            continue
        out.setdefault(r["product"], {})[r["funding_time"]] = (r["funding_time"], rate, px, ix, r["utc_time"])
    return {p: sorted(v.values()) for p, v in out.items()}

def ts(s):  # funding_time like 2026-10-09T12:00:00Z
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)

def avg_apr(rows, hours):
    if not rows: return None, 0
    end = ts(rows[-1][0]); x = [r[1] for r in rows if (end - ts(r[0])).total_seconds() < hours * 3600]
    return (sum(x) / len(x) * HOURS_YR if x else None), len(x)

def load():
    if os.path.exists(STATE_F): return json.load(open(STATE_F))
    return {"cash": START, "pos": {}, "started": None, "funding_total": 0.0, "fees_total": 0.0, "payments": 0}

def event(d, coin, ev, detail):
    new = not os.path.exists(EVENTS_F)
    with open(EVENTS_F, "a", newline="") as f:
        w = csv.writer(f)
        if new: w.writerow(["date", "coin", "event", "detail"])
        w.writerow([d, coin, ev, detail])

def unreal(p, px, ix):
    return p["q"] * (ix - p["ix_e"]) - p["q"] * (px - p["px_e"])

def exit_cost(p, px, ix):
    return p["q"] * ix * SPOT_COST + p["q"] * px * PERP_COST

def equity(s, last):
    e = s["cash"]
    for c, p in s["pos"].items():
        _, _, px, ix, _ = last[c]; e += unreal(p, px, ix) - exit_cost(p, px, ix)
    return e

def main():
    s = load(); log = read_log(); now = datetime.now(timezone.utc); today = now.strftime("%Y-%m-%d")
    if not s["started"]: s["started"] = today
    last, sig = {}, {}
    for c, pid in COINS.items():
        rows = log.get(pid, [])
        if not rows: continue
        last[c] = rows[-1]
        a7, n7 = avg_apr(rows, 168); a3, _ = avg_apr(rows, 72); sig[c] = (a7, a3, n7)
        # 1) collect funding for every new funding hour since the last one booked
        p = s["pos"].get(c)
        if p:
            prev = ts(p["last_ft"])
            for ft, rate, px, ix, _ in rows:
                t = ts(ft)
                if t <= prev: continue
                hours = max(1, round((t - prev).total_seconds() / 3600))   # fills gaps if a run was missed
                cash = p["q"] * px * rate * hours                          # short receives when rate > 0
                s["cash"] += cash; p["funding"] += cash; s["funding_total"] += cash; s["payments"] += hours
                prev = t
            p["last_ft"] = prev.strftime("%Y-%m-%dT%H:%M:%SZ")
    # 2) exits, then entries
    for c in list(s["pos"]):
        a7, a3, n7 = sig.get(c, (None, None, 0))
        if a7 is not None and (a7 < EXIT_APR or (a3 is not None and a3 < 0)):
            p = s["pos"].pop(c); _, _, px, ix, _ = last[c]
            pnl_basis = unreal(p, px, ix); fee = exit_cost(p, px, ix)
            s["cash"] += pnl_basis - fee; s["fees_total"] += fee
            event(today, c, "EXIT", f"funding faded (7d {a7:.1%}/yr, 3d {a3:.1%}/yr). Funding earned ${p['funding']:,.2f}, basis {pnl_basis:+,.2f}, exit fees ${fee:,.2f}")
    eq = equity(s, last)
    for c in COINS:
        if c in s["pos"] or c not in sig: continue
        a7, a3, n7 = sig[c]
        if a7 is None or n7 < 72 or a7 < ENTER_APR: continue
        _, _, px, ix, ft_utc = last[c]
        alloc = eq / len(COINS); notional = alloc / (1 + COLLATERAL); q = notional / ix
        fee = q * ix * SPOT_COST + q * px * PERP_COST
        s["cash"] -= fee; s["fees_total"] += fee
        s["pos"][c] = {"q": q, "ix_e": ix, "px_e": px, "date": today, "last_ft": last[c][0], "funding": 0.0, "fee_in": fee}
        event(today, c, "ENTER", f"buy {q:.4f} {c} spot @ {ix:.4f}, short {q:.4f} on the perp @ {px:.4f} (${notional:,.0f} each leg). "
                                 f"7d funding {a7:.1%}/yr. Entry fees ${fee:,.2f}")
    eq = equity(s, last); s["equity"] = eq; s["updated"] = now.strftime("%Y-%m-%d %H:%M UTC")
    json.dump(s, open(STATE_F, "w"), indent=1)
    # one equity row per UTC day (the latest value of the day)
    rows = list(csv.DictReader(open(EQUITY_F))) if os.path.exists(EQUITY_F) else []
    rows = [r for r in rows if r["date"] != today] + [{"date": today, "equity": f"{eq:.2f}", "funding_total": f"{s['funding_total']:.2f}", "positions": len(s["pos"])}]
    with open(EQUITY_F, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "equity", "funding_total", "positions"]); w.writeheader(); w.writerows(rows)
    report(s, last, sig, rows)
    print(f"Carry book F: equity ${eq:,.2f} | funding collected ${s['funding_total']:,.2f} | fees ${s['fees_total']:,.2f} | positions {', '.join(s['pos']) or 'none'}")

def report(s, last, sig, eqrows):
    try:
        import paper_multi as pm; nav, css = pm.nav("F", "../"), pm.NAV_CSS
    except Exception:
        nav, css = "", ""
    prow = ""
    for c, p in s["pos"].items():
        _, _, px, ix, _ = last[c]; b = unreal(p, px, ix)
        prow += (f"<tr><td>{c}</td><td>{p['date']}</td><td class=n>{p['q']:.4f}</td><td class=n>{p['ix_e']:.4f} / {p['px_e']:.4f}</td>"
                 f"<td class=n>{ix:.4f} / {px:.4f}</td><td class=n>${p['q']*ix:,.0f}</td><td class=n style='color:#5cc98a'>${p['funding']:,.2f}</td>"
                 f"<td class=n>{b:+,.2f}</td><td class=n>-${p['fee_in']:,.2f}</td></tr>")
    srow = ""
    for c in COINS:
        a7, a3, n7 = sig.get(c, (None, None, 0))
        st = "holding" if c in s["pos"] else ("waiting for 3 days of data" if n7 < 72 else ("would enter" if (a7 or 0) >= ENTER_APR else "below 10%/yr: stays out"))
        f = lambda x: "–" if x is None else f"{x:.1%}"
        srow += f"<tr><td>{c}</td><td class=n>{f(a7)}</td><td class=n>{f(a3)}</td><td>{st}</td></tr>"
    ev = list(csv.DictReader(open(EVENTS_F))) if os.path.exists(EVENTS_F) else []
    evrow = "".join(f"<tr><td>{e['date']}</td><td>{e['coin']}</td><td>{e['event']}</td><td>{e['detail']}</td></tr>" for e in reversed(ev[-30:]))
    eqrow = "".join(f"<tr><td>{r['date']}</td><td class=n>${float(r['equity']):,.2f}</td><td class=n>${float(r['funding_total']):,.2f}</td></tr>" for r in reversed(eqrows[-60:]))
    eq = s["equity"]
    html = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60"><title>F: Funding carry</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;background:#111514;color:#eef3f1;max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:26px;margin:0}} h2{{font-size:17px;margin:28px 0 8px}} .muted{{color:#9aa6a0}} .big{{font-size:28px;font-weight:600}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td,th{{padding:6px 8px;border-bottom:1px solid #2a312e;text-align:left;vertical-align:top}}
th{{color:#86918c;font-size:12px;text-transform:uppercase}} .n{{text-align:right;font-family:ui-monospace,monospace;white-space:nowrap}} .wrap{{overflow-x:auto}}
{css}</style>
{nav}
<h1>F: Funding carry (LTC, SUI)</h1>
<p class=muted>PAPER ONLY. Holds each coin outright and shorts the same amount on its perp, so price moves cancel out and the short collects the hourly funding that leveraged buyers pay.
Enters a coin when its 7-day average funding is at least 10%/yr, exits when it drops below 4%/yr or the 3-day average turns negative.
Costs: spot 0.40% + 0.05% slippage per side, perp 0.12% + 0.05% per side. Equity is net of the fees to close everything now. Started {s['started']}. Updated {s.get('updated','')}.</p>
<p><span class=big>${eq:,.2f}</span> <span class=muted>({eq/START-1:+.2%} on $5,000) · funding collected ${s['funding_total']:,.2f} over {s['payments']} hourly payments · fees paid ${s['fees_total']:,.2f}</span></p>
<h2>Positions</h2><div class=wrap><table><tr><th>Coin</th><th>Since</th><th class=n>Coins</th><th class=n>Entry spot / perp</th><th class=n>Now spot / perp</th><th class=n>Each leg</th><th class=n>Funding earned</th><th class=n>Basis P&amp;L</th><th class=n>Entry fees</th></tr>{prow or '<tr><td colspan=9>No positions</td></tr>'}</table></div>
<p class=muted>Basis P&amp;L is the spot gain minus the perp loss (or the reverse). It stays small because the two legs move together.</p>
<h2>Funding signal</h2><div class=wrap><table><tr><th>Coin</th><th class=n>7-day avg, per year</th><th class=n>3-day avg, per year</th><th>Status</th></tr>{srow}</table></div>
<h2>Daily equity</h2><div class=wrap><table><tr><th>Date</th><th class=n>Equity</th><th class=n>Funding collected</th></tr>{eqrow}</table></div>
<h2>Events</h2><div class=wrap><table><tr><th>Date</th><th>Coin</th><th>Event</th><th>Detail</th></tr>{evrow or '<tr><td colspan=4>None yet</td></tr>'}</table></div>"""
    open(REPORT_F, "w", encoding="utf-8").write(html)

if __name__ == "__main__":
    main()
