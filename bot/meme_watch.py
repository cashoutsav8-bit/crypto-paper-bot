#!/usr/bin/env python3
"""
Fast watcher. RESEARCH ONLY: it never trades. Runs between full scans (about 12 minutes at a time) and checks
prices every minute, so an alert reaches your phone within a minute or two of a move.

Alerts:
  BREAKOUT  a coin on the scanner's watchlist (decent score, passed the safety check, not a copycat) jumps 10%+
            in 5 minutes on heavy, mostly-buy volume
  PICK      Claude's picks and coins you were alerted on: waking up, 2x / 5x / 10x from entry (take your money out),
            down 50%, or liquidity pulled (possible rug)
  NEWS      a new headline about a pick (for example Rockstar commenting on Dacity Coin)
"""
import json, os, re, sys, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meme_scanner as ms   # reuses its fetch helpers and formatting
import notify

ms.BUDGET = 10 ** 9           # the scanner's per-run time budget doesn't apply here
OUT = ms.OUT
MINUTES = float(os.environ.get("MEME_WATCH_MINUTES", "12.5"))
POLL = 60
BREAKOUTS_PER_DAY = 6
EARLY_PER_DAY = 8
FRESH_PER_DAY = 6
FRESH_NETS = ["solana", "base"]          # GeckoTerminal networks polled for brand-new pools every minute
BASE_CACHE = {}
FOLLOW_DAYS = 3


def load(name, default):
    try:
        with open(os.path.join(OUT, name)) as f:
            return json.load(f)
    except Exception:
        return default


def build_watch(scan, picks, ledger, book):
    w = {}
    for c in scan.get("top", []):
        sec = (c.get("sec") or {}).get("verdict")
        if (c.get("score", 0) >= 35 and sec in ("pass", "caution") and "NOT the oldest" not in (c.get("copy_note") or "")
                and not ms.flag_note(c) and c.get("liq", 0) >= 50000 and c.get("mcap", 0) < 1e9):
            w[c["key"]] = {"key": c["key"], "symbol": c["symbol"], "name": c["name"], "chain": c["chain"], "address": c["address"],
                           "url": c.get("pair_url"), "score": c["score"], "sec": sec, "role": "scan", "ref_liq": c.get("liq", 0)}
    # radar: every coin with real trading seen in the last 48h, so early moves get caught (safety checked on demand)
    sec_cache = load("security_cache.json", {})
    for k, r in load("radar.json", {}).items():
        if k not in w:
            w[k] = {"key": k, "symbol": r["symbol"], "name": r.get("name") or r["symbol"], "chain": r["chain"], "address": r["address"],
                    "url": f"https://dexscreener.com/{r['chain']}/{r['address']}", "role": "radar", "sec": (sec_cache.get(k) or {}).get("verdict"),
                    "ref_liq": 0}
    # Claude's picks: entry = the paper ticket's entry price
    entries = {t["key"]: t for t in ledger.get("tickets", []) if t["by"] == "claude" and t["status"] == "open"}
    for p in picks:
        if not p.get("address"):
            continue
        k = ms.ckey(p["chain"], p["address"])
        t = entries.get(k)
        w[k] = {**w.get(k, {}), "key": k, "symbol": p["symbol"], "name": p.get("name") or p["symbol"], "chain": p["chain"],
                "address": p["address"], "url": p.get("url") or f"https://dexscreener.com/{p['chain']}/{p['address']}",
                "role": "pick", "entry": (t or {}).get("entry"), "entry_t": (t or {}).get("t"), "ref_liq": w.get(k, {}).get("ref_liq", 0),
                "news_name": re.sub(r"\s*\(.*?\)", "", p.get("name") or p["symbol"]).strip()}
    # your real positions (docs/meme/my_positions.json): entry price + exit plan, no dollar amounts
    for p in load("my_positions.json", []):
        k = ms.ckey(p["chain"], p["address"])
        cur = w.get(k, {})
        w[k] = {**cur, "key": k, "symbol": p["symbol"], "name": p.get("name") or p["symbol"], "chain": p["chain"], "address": p["address"],
                "url": p.get("url") or cur.get("url") or f"https://dexscreener.com/{p['chain']}/{p['address']}", "role": "position",
                "entry": p["entry"], "plan": p, "ref_liq": cur.get("ref_liq", 0), "news_name": cur.get("news_name") or p.get("news_name")}
    # coins you were alerted on in the last few days: follow them for exits
    for a in book.d["log"] + book.other_log:
        if a.get("kind") in ("breakout", "toppick", "early", "sleeper", "fresh") and a.get("coin") and a.get("price") and time.time() - a["t"] < FOLLOW_DAYS * 86400 * (3 if a["kind"] in ("fresh", "sleeper") else 1):
            k = a["coin"]
            if k.startswith("cb:"):
                continue
            ch, addr = k.split(":", 1)
            cur = w.get(k, {})
            if cur.get("role") in ("pick", "position"):
                continue
            w[k] = {**cur, "key": k, "symbol": cur.get("symbol") or a["title"].split("$")[-1].split(" ")[0], "name": cur.get("name", ""),
                    "chain": ch, "address": cur.get("address") or addr, "url": a.get("url") or cur.get("url"), "role": "followed",
                    "entry": a["price"], "entry_t": a["t"], "ref_liq": cur.get("ref_liq", 0), "score": cur.get("score"), "sec": cur.get("sec")}
    return w


def poll(watch):
    by_chain = {}
    for v in watch.values():
        by_chain.setdefault(v["chain"], []).append(v["address"])
    agg = {}
    for ch, addrs in by_chain.items():
        for i in range(0, len(addrs), 30):
            j = ms.fetch(f"{ms.DS}/tokens/v1/{ch}/" + ",".join(addrs[i:i + 30]), "DexScreener watch", tries=2)
            for p in j if isinstance(j, list) else []:
                k = ms.ckey(p.get("chainId"), (p.get("baseToken") or {}).get("address"))
                if k not in watch:
                    continue
                liq = ms.num((p.get("liquidity") or {}).get("usd"))
                vol, tx, pc = p.get("volume") or {}, p.get("txns") or {}, p.get("priceChange") or {}
                if liq > 2e7 and ms.num(vol.get("h24")) < liq * 0.005:
                    continue
                a = agg.setdefault(k, {"liq": -1, "vol_m5": 0, "vol_h1": 0, "vol_h6": 0, "b5": 0, "s5": 0, "b1": 0, "s1": 0, "created": None})
                a["vol_m5"] += ms.num(vol.get("m5"))
                a["vol_h1"] += ms.num(vol.get("h1"))
                a["vol_h6"] += ms.num(vol.get("h6"))
                a["b5"] += int(ms.num((tx.get("m5") or {}).get("buys")))
                a["s5"] += int(ms.num((tx.get("m5") or {}).get("sells")))
                a["b1"] += int(ms.num((tx.get("h1") or {}).get("buys")))
                a["s1"] += int(ms.num((tx.get("h1") or {}).get("sells")))
                cr = ms.num(p.get("pairCreatedAt")) / 1000 or None
                if cr and (not a["created"] or cr < a["created"]):
                    a["created"] = cr
                if (cr and liq >= 1000 and not any(b in (p.get("dexId") or "").lower() for b in ms.BONDING_DEX)
                        and (not a.get("base_created") or cr < a["base_created"])):
                    a["base_created"], a["base_pair"] = cr, p.get("pairAddress")
                if liq > a["liq"]:
                    a["pair_addr"] = p.get("pairAddress")
                    a.update(liq=liq, price=ms.num(p.get("priceUsd")), mcap=ms.num(p.get("marketCap")) or ms.num(p.get("fdv")),
                             m5=ms.num(pc.get("m5")), h1=ms.num(pc.get("h1")), h6=ms.num(pc.get("h6")), h24=ms.num(pc.get("h24")),
                             name=(p.get("baseToken") or {}).get("name"))
    return agg


def chain_name(ch):
    return ms.CHAIN_LABEL.get(ch, ch.title())


def check_position(k, v, a, book, base):
    """Your exit plan, enforced by alerts: sell half at 2x, then a trailing stop on the rest; before that, a stop at your entry."""
    plan, e, sym, url, px = v["plan"], v["entry"], v["symbol"], v.get("url"), a["price"]
    half_x, trail, stop_x = plan.get("sell_half_at", 2.0), plan.get("trail", 0.40), plan.get("stop_at", 1.0)
    st = book.d.setdefault("positions", {}).setdefault(f"{k}:{e}", {"peak": e, "half": None})
    st["peak"], st["last"], st["t"] = max(st["peak"], px), px, time.time()
    mult, tag = px / e, f"pos:{k}:{e}"
    if not st["half"] and mult >= half_x:
        st["half"] = time.time()
        book.alert(tag + ":half", "position", f"💰 SELL HALF: ${sym} is {mult:.1f}x your entry",
                   f"{ms.price_s(e)} → {ms.price_s(px)}. Selling half now gets your original money back and the rest rides for free. "
                   f"From here you'll get a stop alert if it falls {trail * 100:.0f}% from its high. {base}", url, 5, ["moneybag"], k, px)
    elif st["half"] and px <= st["peak"] * (1 - trail) and not book.recently(tag + ":trail", 24):
        book.alert(tag + ":trail", "position", f"🛑 TRAILING STOP: ${sym} is down {(1 - px / st['peak']) * 100:.0f}% from its high",
                   f"High {ms.price_s(st['peak'])} → now {ms.price_s(px)} ({mult:.1f}x your entry). The plan says sell the rest now. {base}",
                   url, 5, ["octagonal_sign"], k, px)
    elif not st["half"] and mult <= stop_x and not book.recently(tag + ":stop", 12):
        book.alert(tag + ":stop", "position", f"🛑 STOP: ${sym} is back to your entry ({ms.price_s(px)})",
                   f"It was up to {st['peak'] / e:.1f}x. The plan says sell: don't let a winner turn into a loss. {base}", url, 5, ["octagonal_sign"], k, px)
    # a real, sellable spike: the main pool jumped 25%+ in 5 minutes (not a one-off bad print from a junk pool)
    if a.get("m5", 0) >= 25 and a.get("liq", 0) >= 50000 and not book.recently(tag + ":spike", 1):
        book.alert(tag + ":spike", "position", f"⚡ ${sym} spiking: +{a['m5']:.0f}% in 5 min",
                   f"Now {ms.price_s(px)} ({mult:.1f}x your entry) in the main pool ({ms.usd(a['liq'])} liquidity). Spikes like this often reverse "
                   f"within minutes; if you want to sell into it, do it now. {base}", url, 5, ["zap"], k, px)
    for lvl in (10, 5, 3):
        if mult >= lvl:
            if not book.recently(f"{tag}:x{lvl}", 24 * 60):
                book.alert(f"{tag}:x{lvl}", "position", f"🚀 ${sym} is {lvl}x your entry",
                           f"{ms.price_s(e)} → {ms.price_s(px)}. Consider selling another chunk; runs like this rarely hold. {base}", url, 5, ["rocket"], k, px)
            break
    day = time.strftime("%Y-%m-%d", time.localtime())
    if plan.get("remind_on") and day >= plan["remind_on"] and not book.recently(tag + ":remind", 24 * 60):
        book.alert(tag + ":remind", "position", f"⏰ ${sym}: {plan.get('remind_title', 'check your plan')}", plan.get("remind_note", "") + f" Now {mult:.1f}x your entry.",
                   url, 5, ["alarm_clock"], k, px)


SEC_CACHE = {}


def base_for(v, a):
    """Has this coin already had its big run this week? (hourly closes of its main pool)"""
    if not BASE_CACHE:
        BASE_CACHE.update({k: e for k, e in load("base_cache.json", {}).items() if "high_after" in e})
        BASE_CACHE["_loaded"] = {"t": 0}
    return ms.base_check(v["chain"], a.get("base_pair") or a.get("pair_addr"), BASE_CACHE, a.get("price"), "GeckoTerminal OHLCV (watch)")


def fresh_feed(watch, book):
    """Brand-new pools (mostly fresh pump.fun graduations) join the watch list for their first 6 hours."""
    fr = book.d.setdefault("fresh", {})
    now = time.time()
    for net in FRESH_NETS:
        j = ms.fetch(f"{ms.GT}/networks/{net}/new_pools?include=base_token&page=1", "GeckoTerminal new pools", headers=ms.GTH, tries=1)
        if not isinstance(j, dict):
            continue
        toks = {t.get("id"): t.get("attributes") or {} for t in j.get("included") or [] if t.get("type") == "token"}
        for d in j.get("data") or []:
            at, rel = d.get("attributes") or {}, d.get("relationships") or {}
            bid = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
            dex = (((rel.get("dex") or {}).get("data") or {}).get("id") or "").lower()
            if "_" not in bid or "pump-fun" in dex or "pumpfun" == dex:        # skip bonding-curve "pools"
                continue
            addr, chain = bid.rsplit("_", 1)[1], ms.GT2DS.get(net, net)
            sym = (toks.get(bid, {}).get("symbol") or "").strip()
            if not sym or sym.upper() in ms.MAJORS:
                continue
            k = ms.ckey(chain, addr)
            if k in fr or ms.num(at.get("reserve_in_usd")) < 8000:
                continue
            fr[k] = {"symbol": sym, "name": toks.get(bid, {}).get("name") or sym, "chain": chain, "address": addr,
                     "created": ms.parse_time(at.get("pool_created_at")) or now,
                     "mcap0": ms.num(at.get("market_cap_usd")) or ms.num(at.get("fdv_usd")), "seen": now}
    for k in [k for k, r in fr.items() if now - r["created"] > 6 * 3600]:
        del fr[k]
    for k, r in fr.items():
        if k not in watch:
            watch[k] = {"key": k, "symbol": r["symbol"], "name": r["name"], "chain": r["chain"], "address": r["address"],
                        "url": f"https://dexscreener.com/{r['chain']}/{r['address']}", "role": "fresh", "created": r["created"],
                        "mcap0": r["mcap0"], "ref_liq": 0}


def check_fresh(k, v, a, book):
    """A launch that is running but hasn't had its big leg yet: 1.5-4x since it listed, real buyers, still rising."""
    age_m = (time.time() - (v.get("created") or time.time())) / 60
    mcap0 = v.get("mcap0") or 0
    if not mcap0:                                   # first sighting without a starting mcap: use the first price we see
        v["mcap0"] = mcap0 = a["mcap"]
        book.d["fresh"].get(k, {})["mcap0"] = mcap0
    growth = a["mcap"] / mcap0 if mcap0 else 0
    n1 = a["b1"] + a["s1"]
    if not (15 <= age_m <= 240 and 1.5 <= growth <= 4 and 5e4 <= a["mcap"] <= 1.5e6 and a["liq"] >= 15000 and a["vol_h1"] >= 25000
            and n1 >= 80 and a["b1"] / n1 >= 0.6 and a["m5"] >= 0):
        return
    if book.recently("fresh:" + k, 24) or book.count_today("fresh") >= FRESH_PER_DAY:
        return
    if not safe(k, v) or v["sec"] != "pass":         # launches need a clean check: no mint/freeze, holders not concentrated
        return
    book.alert("fresh:" + k, "fresh", f"🐣 New launch running: ${v['symbol']} {growth:.1f}x since it listed {age_m:.0f}m ago",
               f"{v['name']} on {chain_name(v['chain'])}: mcap {ms.usd(a['mcap'])}, liquidity {ms.usd(a['liq'])}, {ms.usd(a['vol_h1'])} traded in the last hour, "
               f"{a['b1'] / n1 * 100:.0f}% buys, safety pass.\nContract: {v['address']}\n"
               "Highest-risk alert: most launches dump within the hour. $10-25 max, sell half at 2x.", v["url"], 5, ["hatching_chick"], k, a["price"])


def safe(k, v):
    """Safety verdict for a coin, checked on demand (RugCheck / GoPlus) the first time it matters."""
    if v.get("sec") in ("pass", "caution"):
        return True
    if v.get("sec") in ("danger", "unchecked") or v.get("sec_tried"):
        return False
    v["sec_tried"] = True
    if not SEC_CACHE:
        SEC_CACHE.update(load("security_cache.json", {}))
    res = ms.security({"key": k, "chain": v["chain"], "address": v["address"]}, SEC_CACHE)
    v["sec"] = res.get("verdict")
    return v["sec"] in ("pass", "caution")


def base_of(v, a):
    return (f"Mcap {ms.usd(a['mcap'])}, liquidity {ms.usd(a['liq'])}" + (f", safety {v['sec']}" if v.get("sec") else "")
            + (f", scanner score {v['score']}" if v.get("score") else "") + f".\nContract: {v['address']}")


def check(watch, agg, book):
    for k, v in watch.items():
        a = agg.get(k)
        if not a or not a.get("price"):
            continue
        sym, url = v["symbol"], v.get("url")
        n5 = a["b5"] + a["s5"]
        buys = a["b5"] / n5 if n5 else 0
        pace = a["vol_h1"] / 12 if a["vol_h1"] > 0 else 0
        surge = a["vol_m5"] / pace if pace > 0 else 0
        base = (f"Mcap {ms.usd(a['mcap'])}, liquidity {ms.usd(a['liq'])}"
                + (f", safety {v['sec']}" if v.get("sec") else "") + (f", scanner score {v['score']}" if v.get("score") else "")
                + f".\nContract: {v['address']}")
        v["ref_liq"] = max(v.get("ref_liq") or 0, a["liq"])
        # liquidity pulled: the classic rug
        if v["role"] in ("pick", "followed", "position") and v["ref_liq"] > 20000 and a["liq"] < 0.4 * v["ref_liq"] and not book.recently("rug:" + k, 12):
            book.alert("rug:" + k, "rug", f"⚠️ ${sym}: liquidity pulled ({ms.usd(v['ref_liq'])} → {ms.usd(a['liq'])})",
                       f"{v['name']} on {chain_name(v['chain'])}. Liquidity dropped over 60%, which is how rugs look. If you hold it, "
                       f"try to sell now. {base}", url, 5, ["warning"], k, a["price"])
        if v["role"] == "position":
            check_position(k, v, a, book, base)
            continue
        # exits for coins you might be holding
        if v.get("entry") and v["role"] in ("pick", "followed"):
            mult = a["price"] / v["entry"]
            tag = f"{k}:{int(v.get('entry_t') or 0)}"
            for lvl in (10, 5, 2):
                if mult >= lvl:
                    if not book.recently(f"x{lvl}:{tag}", 24 * 30):
                        tip = ("Plan: sell enough to take your original money out; let the rest ride."
                               if lvl == 2 else "Consider taking more off the table; these rarely hold.")
                        book.alert(f"x{lvl}:{tag}", "exit", f"💰 ${sym} is {mult:.1f}x since " + ("Claude's pick" if v["role"] == "pick" else "the alert"),
                                   f"{v['name']} on {chain_name(v['chain'])}: {ms.price_s(v['entry'])} → {ms.price_s(a['price'])}. {tip} {base}",
                                   url, 5, ["moneybag"], k, a["price"])
                    break
            if mult <= 0.5 and not book.recently(f"half:{tag}", 24 * 30):
                book.alert(f"half:{tag}", "exit", f"📉 ${sym} is down {(1 - mult) * 100:.0f}% since " + ("Claude's pick" if v["role"] == "pick" else "the alert"),
                           f"{v['name']}: {ms.price_s(v['entry'])} → {ms.price_s(a['price'])}. Decide now whether you're riding it to zero. {base}",
                           url, 4, ["chart_with_downwards_trend"], k, a["price"])
        # a pick waking up: the re-entry signal
        if (v["role"] == "pick" and a["m5"] >= 8 and a["vol_m5"] >= 10000 and surge >= 3 and n5 >= 20 and buys >= 0.6
                and not book.recently("wake:" + k, 2)):
            book.alert("wake:" + k, "wake", f"👀 ${sym} waking up: +{a['m5']:.0f}% in 5 min",
                       f"{v['name']} on {chain_name(v['chain'])}: {ms.usd(a['vol_m5'])} traded in 5 min ({surge:.1f}x its hourly pace), "
                       f"{buys * 100:.0f}% buys. {base}", url, 5, ["eyes"], k, a["price"])
        # peak since each alert on a followed coin (for the "are the alerts any good?" scorecard)
        if v["role"] == "followed" and v.get("entry_t"):
            pk = book.d.setdefault("peaks", {})
            pkey = f"{k}:{int(v['entry_t'])}"
            pk[pkey] = max(pk.get(pkey, 0), a["price"])
        if v["role"] == "fresh":
            check_fresh(k, v, a, book)
            continue
        if v["role"] not in ("scan", "radar"):
            continue
        age = (time.time() - a["created"]) / 3600 if a.get("created") else None
        # early mover: building before the big candle
        reason = ms.early_signal({"m5": a["m5"], "h1": a["h1"], "h6": a.get("h6", 0), "h24": a["h24"]},
                                 {"h1": a["vol_h1"], "h6": a["vol_h6"]}, (a["b1"], a["s1"]), a["liq"], a["mcap"], age)
        bc = None
        if reason and not book.recently("early:" + k, 12):
            bc = ms.base_class(base_for(v, a))
        kind = "sleeper" if bc == "sleeper" else "early"
        if reason and bc in ("sleeper", "mid") and not book.recently("early:" + k, 12) and book.count_today(kind) < EARLY_PER_DAY and safe(k, v):
            b = base_for(v, a) or {}
            book.alert("early:" + k, kind, (f"💤 Sleeper waking: ${sym} +{a['h1']:.0f}% in 1h, only {b.get('now_x', 0):.1f}x off its low"
                                           if kind == "sleeper" else f"🌱 Early mover: ${sym} +{a['h1']:.0f}% in 1h"),
                       f"{v.get('name') or a.get('name') or sym} on {chain_name(v['chain'])}: {reason}. {base_of(v, a)}\n"
                       "Early alerts catch some runs before the big candle, and many that never come. Small size only.",
                       url, 5, ["seedling"], k, a["price"])
        # breakout: a sharp 5-minute jump on heavy buying, but not once it has already run (that's chasing)
        extended = a["h1"] >= 100 or a.get("h6", 0) >= 200
        if (a["m5"] >= 10 and a["vol_m5"] >= max(15000, 3 * pace) and n5 >= 30 and buys >= 0.6 and not extended
                and a["liq"] >= 50000 and a["h24"] < 300 and not book.recently("breakout:" + k, 6)
                and book.count_today("breakout") < BREAKOUTS_PER_DAY and safe(k, v) and ms.base_class(base_for(v, a)) in ("sleeper", "mid")):
            book.alert("breakout:" + k, "breakout", f"🚀 Breakout: ${sym} +{a['m5']:.0f}% in 5 min",
                       f"{v['name']} on {chain_name(v['chain'])}: {ms.usd(a['vol_m5'])} traded in 5 min ({surge:.1f}x normal), "
                       f"{buys * 100:.0f}% buys. {base}\nMost breakouts fade within the hour; only money you can lose.",
                       url, 5, ["rocket"], k, a["price"])


NEWS_EXTRA = ['Rockstar "Dacity"', 'Rockstar "meme coin"', '"Take-Two" crypto token']


def check_news(watch, book):
    names = sorted({v["news_name"] for v in watch.values() if v.get("role") in ("pick", "position") and v.get("news_name")})
    queries = [f'"{n}"' for n in names] + NEWS_EXTRA
    first_ever = not book.d["seen_news"]
    seen = set(book.d["seen_news"])
    for q in queries:
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d") + "&hl=en-US&gl=US&ceid=US:en"
        for it in ms.parse_feed(ms.fetch(url, "Google News watch", kind="text")):
            title = it["title"]
            norm = re.sub(r"[^a-z0-9]", "", title.lower())[:120]
            if not norm or norm in seen:
                continue
            seen.add(norm)
            book.d["seen_news"].append(norm)
            low = title.lower()
            pick = next((n for n in names if n.lower() in low), None)
            official = bool(re.search(r"rockstar|take-two|take two", low) and re.search(r"coin|crypto|token", low)
                            and re.search(r"respond|statement|comment|confirm|den(y|ies)|warn|sue|lawsuit|cease|official|clarif|not affiliated|scam", low))
            fresh = it["t"] and time.time() - it["t"] < 3 * 3600
            if not fresh or first_ever or not (pick or official) or book.count_today("news") >= 5:
                continue
            # many outlets re-run the same story: one alert per pick every 6 hours, unless it's Rockstar/Take-Two reacting
            key = "news:official:" + norm[:50] if official else "news:" + (pick or "").lower()
            if book.recently(key, 6):
                continue
            book.alert(key, "news", ("🚨 " if official else "📰 ") + title[:110], f"{title}\n(Google News, query {q})",
                       ms.safe_url(it["link"]), 5 if official else 3, ["rotating_light" if official else "newspaper"])
    book.d["seen_news"] = book.d["seen_news"][-400:]


def main():
    scan = load("scan.json", {})
    picks = load("claude_picks.json", [])
    ledger = load("ledger.json", {"tickets": []})
    book = notify.AlertBook(os.path.join(OUT, "watch.json"), [os.path.join(OUT, "alerts.json")])
    other = load("alerts.json", {})
    book.other_log = other.get("log", [])
    watch = build_watch(scan, picks, ledger, book)
    print(f"watching {len(watch)} coins for {MINUTES} min; push alerts {'ON' if notify.TOPIC else 'OFF (no NTFY_TOPIC secret)'}")
    end = time.time() + MINUTES * 60
    last_news = 0
    n = 0
    while time.time() < end:
        t0 = time.time()
        try:
            fresh_feed(watch, book)
            check(watch, poll(watch), book)
            if time.time() - last_news > 300:
                check_news(watch, book)
                last_news = time.time()
        except Exception as e:
            print("watch error:", type(e).__name__, e)
        n += 1
        book.d["last_poll"] = time.time()
        book.d["polls"] = n
        book.save()
        time.sleep(max(1, POLL - (time.time() - t0)))
    book.save()
    print(f"done: {n} polls")


if __name__ == "__main__":
    main()
