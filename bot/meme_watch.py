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
    # coins you were alerted on in the last few days: follow them for exits
    for a in book.d["log"] + book.other_log:
        if a.get("kind") in ("breakout", "toppick") and a.get("coin") and a.get("price") and time.time() - a["t"] < FOLLOW_DAYS * 86400:
            k = a["coin"]
            if k.startswith("cb:"):
                continue
            ch, addr = k.split(":", 1)
            cur = w.get(k, {})
            if cur.get("role") == "pick":
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
                a = agg.setdefault(k, {"liq": -1, "vol_m5": 0, "vol_h1": 0, "b5": 0, "s5": 0})
                a["vol_m5"] += ms.num(vol.get("m5"))
                a["vol_h1"] += ms.num(vol.get("h1"))
                a["b5"] += int(ms.num((tx.get("m5") or {}).get("buys")))
                a["s5"] += int(ms.num((tx.get("m5") or {}).get("sells")))
                if liq > a["liq"]:
                    a.update(liq=liq, price=ms.num(p.get("priceUsd")), mcap=ms.num(p.get("marketCap")) or ms.num(p.get("fdv")),
                             m5=ms.num(pc.get("m5")), h1=ms.num(pc.get("h1")), h24=ms.num(pc.get("h24")))
    return agg


def chain_name(ch):
    return ms.CHAIN_LABEL.get(ch, ch.title())


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
        if v["role"] in ("pick", "followed") and v["ref_liq"] > 20000 and a["liq"] < 0.4 * v["ref_liq"] and not book.recently("rug:" + k, 12):
            book.alert("rug:" + k, "rug", f"⚠️ ${sym}: liquidity pulled ({ms.usd(v['ref_liq'])} → {ms.usd(a['liq'])})",
                       f"{v['name']} on {chain_name(v['chain'])}. Liquidity dropped over 60%, which is how rugs look. If you hold it, "
                       f"try to sell now. {base}", url, 5, ["warning"], k, a["price"])
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
        # breakout on a watchlist coin
        if (v["role"] == "scan" and a["m5"] >= 10 and a["vol_m5"] >= max(15000, 3 * pace) and n5 >= 30 and buys >= 0.6
                and a["liq"] >= 50000 and a["h24"] < 300 and not book.recently("breakout:" + k, 6)
                and book.count_today("breakout") < BREAKOUTS_PER_DAY):
            book.alert("breakout:" + k, "breakout", f"🚀 Breakout: ${sym} +{a['m5']:.0f}% in 5 min",
                       f"{v['name']} on {chain_name(v['chain'])}: {ms.usd(a['vol_m5'])} traded in 5 min ({surge:.1f}x normal), "
                       f"{buys * 100:.0f}% buys. {base}\nMost breakouts fade within the hour; only money you can lose.",
                       url, 5, ["rocket"], k, a["price"])


NEWS_EXTRA = ['Rockstar "Dacity"', 'Rockstar "meme coin"', '"Take-Two" crypto token']


def check_news(watch, book):
    names = sorted({v["news_name"] for v in watch.values() if v.get("role") == "pick" and v.get("news_name")})
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
