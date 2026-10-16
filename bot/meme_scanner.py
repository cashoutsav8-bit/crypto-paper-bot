#!/usr/bin/env python3
"""
Meme coin scanner. RESEARCH + PAPER ONLY: it never places orders, holds no keys and connects to no wallet.

Every run (about every 15 minutes on GitHub):
  1. Finds candidates: DexScreener (boosts, new profiles, community takeovers, trending narratives, searches),
     GeckoTerminal trending/new pools, CoinGecko trending + meme category, Coinbase spot listings.
  2. Reads chatter: 4chan /biz/, Reddit meme-coin subs, crypto news RSS, Google News, Google Trends (US).
  3. Links each coin to narratives and upcoming catalysts (GTA VI, Halloween, midterms...) and to what is
     trending in mainstream culture right now.
  4. Checks rug risk (RugCheck for Solana, GoPlus for EVM chains).
  5. Scores 0-100, writes docs/meme/index.html + scan.json, and paper-buys $100 "tickets" in the strongest
     picks so we can see over weeks whether the scanner actually finds winners.

Standard library only. Every source is optional: if one is down or blocked, the scan carries on without it.
"""
import html, json, math, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("MEME_DATA") or os.path.join(os.path.dirname(HERE), "docs", "meme")
os.makedirs(OUT, exist_ok=True)
START = time.time()
NOW = START
BUDGET = float(os.environ.get("MEME_BUDGET", "330"))   # seconds; sources past this are skipped
ET_TZ = ZoneInfo("America/New_York")
SITE = "https://cashoutsav8-bit.github.io/"

UA = "Mozilla/5.0 (X11; Linux x86_64) meme-scanner/1.0 (research bot; github.com/cashoutsav8-bit/crypto-paper-bot)"
REDDIT_UA = "linux:meme-scanner:1.0 (research bot by cashoutsav8-bit)"

TICKET_USD = 100.0
TICKET_DAYS = 14
COST_EACH_WAY = 0.02          # swap fee + slippage + network, each way
BOT_TICKETS_PER_DAY = 5
CB_TICKETS_PER_DAY = 2
PICK_SCORE = 50               # DEX coin needs this score to get a paper ticket
CB_PICK_HEAT = 70

# ------------------------------------------------------------------ narratives
# Themes are matched against each coin's name, ticker, description and links. A theme with an upcoming
# catalyst earns more points the closer the date gets (memes tend to run INTO an event and dump after).
THEMES = {
    "gaming":   {"label": "Gaming / GTA VI", "kw": [r"gta", r"gta ?(6|vi)", r"vice ?city", r"rockstar", r"gamers?", r"gaming", r"playstation", r"xbox", r"nintendo", r"cyberleek", r"lucia", r"leonida", r"dacity", r"audacity"]},
    "spooky":   {"label": "Halloween", "kw": [r"halloween", r"spooky", r"pumpkin", r"ghosts?", r"witch(es)?", r"zombies?", r"skeletons?", r"vampires?", r"jack ?o ?lantern"]},
    "politics": {"label": "US midterms / politics", "kw": [r"trump", r"maga", r"midterms?", r"elections?", r"senate", r"congress", r"republicans?", r"democrats?", r"vance", r"potus", r"white ?house"]},
    "holiday":  {"label": "Thanksgiving / Christmas", "kw": [r"thanksgiving", r"turkey", r"santa", r"christmas", r"xmas", r"grinch", r"reindeer"]},
    "ai":       {"label": "AI / agents", "kw": [r"ai", r"agents?", r"gpt", r"llm", r"grok", r"openai", r"agi", r"neural"]},
    "elon":     {"label": "Elon / X", "kw": [r"elon", r"musk", r"tesla", r"spacex", r"grok", r"x\.ai"]},
    "robinhood": {"label": "Robinhood Chain", "kw": [r"robinhood", r"vrax", r"tenev", r"hood ?chain"], "chains": ["robinhood", "robinhoodchain", "robinhood-chain"]},
    "stonks":   {"label": "Tokenized-stock memes", "kw": [r"stonks?", r"wall ?street", r"wsb", r"nasdaq"]},
    "animals":  {"label": "Animal memes", "kw": [r"dogs?", r"doge", r"inu", r"shib", r"cats?", r"kitty", r"frogs?", r"pepe", r"monkey", r"ape", r"penguin", r"hippo", r"bonk"]},
}
CATALYSTS = [
    {"date": "2026-10-31", "event": "Halloween", "theme": "spooky"},
    {"date": "2026-11-03", "event": "US midterm elections", "theme": "politics"},
    {"date": "2026-11-19", "event": "GTA VI launch", "theme": "gaming"},
    {"date": "2026-11-26", "event": "Thanksgiving", "theme": "holiday"},
    {"date": "2026-12-25", "event": "Christmas", "theme": "holiday"},
]
# DexScreener searches run every scan, so new coins riding these narratives show up early.
THEME_SEARCH = {"gta": ["gta"], "gta 6": ["gta"], "vice city": ["vice"], "cyberleek": ["leek"], "dacity": ["dacity"], "audacity": ["dacity"], "halloween": ["hallow", "spook"],
                "pumpkin": ["pumpkin"], "midterm": ["midterm", "elect"], "vrax": ["vrax"], "robinhood": ["robin", "hood"]}
# Coins with a known problem. They are shown with the note and never get a paper ticket.
MANUAL_FLAGS = {
    "cyberleek": "Creator wallet already sold about $250k on Aug 27 (Tom's Hardware). Pumps are driven by threats to leak stolen GTA VI files; fell ~50% in a day on Oct 12 (Kotaku).",
}
# Facts worth knowing about specific coins (shown as a chip; adds narrative points). Keyed by lowercase ticker or name.
MANUAL_NOTES = {
    "dacity": "'Dacity Coin' is shown on a plane in the official GTA VI launch trailer (Oct 15); Rockstar hasn't commented",
}
THEME_RE = {k: re.compile(r"(?<![a-z0-9])(" + "|".join(v["kw"]) + r")(?![a-z0-9])", re.I) for k, v in THEMES.items()}

MAJORS = {"SOL", "WSOL", "ETH", "WETH", "BTC", "WBTC", "CBBTC", "USDC", "USDT", "DAI", "USDS", "USD1", "PYUSD", "BNB", "WBNB", "USDE",
          "FDUSD", "TUSD", "USDB", "JITOSOL", "MSOL", "STETH", "WSTETH", "CBETH", "RETH", "EURC", "AVAX", "WAVAX", "POL", "MATIC",
          "WPOL", "ARB", "OP", "USDBC", "SUSDE", "JUPSOL", "BSOL", "INF", "HYPE", "WHYPE", "XAUT", "PAXG"}
STOP = set("""THE AND FOR YOU ARE BUT NOT ALL ANY CAN HAD HER WAS ONE OUR OUT DAY GET HAS HIM HIS HOW MAN NEW NOW OLD SEE TWO WAY WHO BOY
DID ITS LET PUT SAY SHE TOO USE THIS THAT WITH HAVE FROM THEY WILL YOUR WHAT WHEN MAKE LIKE TIME JUST KNOW TAKE INTO YEAR GOOD SOME
THEM THAN THEN LOOK ONLY COME OVER ALSO BACK WORK WELL EVEN WANT BECAUSE ANY THESE GIVE MOST VERY BUY SELL HOLD PUMP DUMP MOON
LONG SHORT CALL PUTS CALLS USD USDT USDC BTC ETH SOL CEO NFT NFTS ATH ATL LOL LMAO KEK FUD DYOR NGMI WAGMI GMI IMO TLDR EDIT
DEX CEX APE APED APES RUG RUGS RUGGED DEV DEVS LFG GEM GEMS SHILL JEET JEETS CTO CA MC MCAP FDV LP TVL APY APR ROI PNL OTC IPO
SEC FBI CIA USA NYC UK EU AI API APP URL HTTP HTTPS WWW COM ORG NET NOT YES OMG WTF IRL TBH IDK FYI ASAP EOD AMA GPT LLM TA KYC
DCA BULL BEAR BULLISH BEARISH RIP DEAD SOON NEXT FIRST LAST WEEK MONTH TODAY TOMORROW HERE THERE WHERE WHY ANON ANONS OP FAG
NIGGER JEW SHIT FUCK FUCKING CRYPTO COIN COINS TOKEN TOKENS MEME MEMES MEMECOIN MEMECOINS CHART CHARTS PRICE PRICES TRADE TRADING
MONEY CASH GAIN GAINS LOSS LOSE LOST PROFIT REAL FAKE TRUE FALSE BEST WORST HIGH LOW TOP BOTTOM BIG SMALL FREE FULL SAFE RISK
GOING GONNA WANNA GOT GETS GIVE GAVE MADE SAID SAYS SAME EVERY MUCH MORE LESS MANY STILL NEVER ALWAYS AGAIN AFTER BEFORE
RAYDIUM PUMPFUN PUMPSWAP JUPITER METEORA ORCA UNISWAP BASE SOLANA ETHEREUM BINANCE COINBASE ROBINHOOD KRAKEN BYBIT OKX MEXC
BITGET PHANTOM METAMASK TELEGRAM TWITTER DISCORD REDDIT BIZ SMG CMG GENERAL THREAD POST POSTS NEWS AMERICA TRUMP ELON""".split())

SRC_W = {"biz": 1.0, "reddit": 1.5, "news": 3.0}
GT2DS = {"eth": "ethereum", "bsc": "bsc", "base": "base", "solana": "solana", "arbitrum": "arbitrum", "polygon_pos": "polygon",
         "avax": "avalanche", "sui-network": "sui", "ton": "ton", "tron": "tron", "optimism": "optimism", "blast": "blast",
         "sonic": "sonic", "unichain": "unichain", "abstract": "abstract", "hyperevm": "hyperevm", "berachain": "berachain"}
EVM_ID = {"ethereum": 1, "bsc": 56, "base": 8453, "arbitrum": 42161, "polygon": 137, "avalanche": 43114, "optimism": 10,
          "blast": 81457, "sonic": 146, "unichain": 130, "robinhood": 4663}
CHAIN_LABEL = {"solana": "Solana", "ethereum": "Ethereum", "base": "Base", "bsc": "BNB Chain", "arbitrum": "Arbitrum",
               "polygon": "Polygon", "avalanche": "Avalanche", "sui": "Sui", "ton": "TON", "tron": "Tron", "hyperevm": "HyperEVM",
               "abstract": "Abstract", "robinhood": "Robinhood Chain"}
CASE_SENSITIVE_CHAINS = {"solana", "sui", "ton", "tron"}

CASH_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,11})\b")
UP_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,11}\b")
SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

# ------------------------------------------------------------------ http
HEALTH = {}
_hl = threading.Lock()
_host_lock, _host_last = {}, {}
PACE = {"api.geckoterminal.com": 3.0, "api.coingecko.com": 4.0, "api.rugcheck.xyz": 0.7, "api.gopluslabs.io": 1.1,
        "www.reddit.com": 1.2, "old.reddit.com": 1.2, "api.dexscreener.com": 0.22, "a.4cdn.org": 1.1, "api.coinbase.com": 0.12}


def _mark(src, ok, note=""):
    with _hl:
        h = HEALTH.setdefault(src, {"ok": 0, "fail": 0, "note": ""})
        h["ok" if ok else "fail"] += 1
        if note and not ok:
            h["note"] = note[:140]


def fetch(url, src, kind="json", timeout=15, tries=2, headers=None):
    if time.time() - START > BUDGET:
        _mark(src, False, "skipped: out of time this run")
        return None
    host = urllib.parse.urlparse(url).netloc
    with _hl:
        lock = _host_lock.setdefault(host, threading.Lock())
    hdr = {"User-Agent": UA, "Accept": "application/json" if kind == "json" else "*/*"}
    if headers:
        hdr.update(headers)
    note = ""
    for k in range(tries):
        with lock:
            wait = PACE.get(host, 0.3) - (time.time() - _host_last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            _host_last[host] = time.time()
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            text = raw.decode("utf-8", "replace")
            data = json.loads(text) if kind == "json" else text
            _mark(src, True)
            return data
        except urllib.error.HTTPError as e:
            note = f"HTTP {e.code}"
            if e.code in (401, 403, 404, 410):
                break
            if e.code == 429:
                time.sleep(5 + 5 * k)
        except Exception as e:
            note = f"{type(e).__name__}: {str(e)[:90]}"
        if k < tries - 1:
            time.sleep(1.5 + 2 * k)
    _mark(src, False, note)
    return None


# ------------------------------------------------------------------ utils
def num(x, d=0.0):
    try:
        if x is None:
            return d
        if isinstance(x, dict):
            x = x.get("h24", x.get("usd"))
        if isinstance(x, str):
            x = x.replace("$", "").replace(",", "").strip()
        v = float(x)
        return v if math.isfinite(v) else d
    except Exception:
        return d


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def load(name, default):
    try:
        with open(os.path.join(OUT, name)) as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj, pretty=False):
    p = os.path.join(OUT, name)
    with open(p + ".tmp", "w") as f:
        if pretty:
            json.dump(obj, f, indent=1, default=list)
        else:
            json.dump(obj, f, separators=(",", ":"), default=list)
    os.replace(p + ".tmp", p)


def ckey(chain, addr):
    chain = (chain or "").lower()
    return f"{chain}:{addr if chain in CASE_SENSITIVE_CHAINS else (addr or '').lower()}"


def clean(s):
    s = re.sub(r"<br\s*/?>", " ", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_time(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_feed(text):
    out = []
    if not text:
        return out
    try:
        root = ET.fromstring(text)
    except Exception:
        try:
            root = ET.fromstring(re.sub("[^\x09\x0a\x0d\x20-퟿-�]", "", text))
        except Exception:
            return out
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        d = {"title": "", "desc": "", "link": "", "t": None, "traffic": "", "extra": []}
        for ch in el.iter():
            tg = ch.tag.split("}")[-1]
            tx = (ch.text or "").strip()
            if tg == "title" and not d["title"]:
                d["title"] = clean(tx)
            elif tg in ("description", "summary", "content", "encoded") and not d["desc"]:
                d["desc"] = clean(tx)[:1500]
            elif tg == "link" and not d["link"]:
                d["link"] = tx or ch.attrib.get("href", "")
            elif tg in ("pubDate", "published", "updated") and not d["t"]:
                d["t"] = parse_time(tx)
            elif tg == "approx_traffic":
                d["traffic"] = tx
            elif tg == "news_item_title":
                d["extra"].append(clean(tx))
        out.append(d)
    return out


def safe_url(u):
    u = (u or "").strip()
    return u if u.startswith("https://") or u.startswith("http://") else ""


# ------------------------------------------------------------------ candidates
C = {}


def cand(chain, addr):
    k = ckey(chain, addr)
    c = C.get(k)
    if c is None:
        c = C[k] = {"key": k, "chain": (chain or "").lower(), "address": addr, "symbol": "", "name": "", "src": {}, "themes": set(),
                    "metas": [], "links": [], "_pairs": set(), "vol": dict.fromkeys(("m5", "h1", "h6", "h24"), 0.0),
                    "txns": {k2: [0, 0] for k2 in ("m5", "h1", "h6", "h24")}, "chg": dict.fromkeys(("m5", "h1", "h6", "h24"), 0.0),
                    "liq": -1.0, "liq_total": 0.0, "price": 0.0, "mcap": 0.0, "created": None, "has_data": False, "boosts": 0}
    return c


def tag(c, source, n=1):
    c["src"][source] = c["src"].get(source, 0) + n


def add_links(c, links):
    for l in links or []:
        u = safe_url(l.get("url") if isinstance(l, dict) else l)
        if u and u not in c["links"] and len(c["links"]) < 8:
            c["links"].append(u)


STOCK_RE = re.compile(r"(\binc\.?$|\bcorp(oration)?\.?$|tokenized|xstock|\bplc$|\bltd\.?$|class [ab] |securities|pre-?ipo)", re.I)


def is_stock(name, sym):
    """Tokenized stocks (Apple Inc., TSLAx...) trade on DEXes too; they aren't memes."""
    return bool(STOCK_RE.search((name or "").strip()))


def take_pair(p, source=None):
    """Merge one DexScreener pair into its base token's candidate (volumes summed over pools, price from the deepest pool)."""
    try:
        bt = p.get("baseToken") or {}
        chain, addr = p.get("chainId"), bt.get("address")
        sym = (bt.get("symbol") or "").strip()
        if not chain or not addr or not sym or sym.upper() in MAJORS or is_stock(bt.get("name"), sym):
            return None
        c = cand(chain, addr)
        if source:
            tag(c, source)
        pa = p.get("pairAddress") or p.get("url")
        if pa in c["_pairs"]:
            return c
        c["_pairs"].add(pa)
        vol, tx = p.get("volume") or {}, p.get("txns") or {}
        if num((p.get("liquidity") or {}).get("usd")) > 2e7 and num(vol.get("h24")) < num((p.get("liquidity") or {}).get("usd")) * 0.005:
            return c
        for k in ("m5", "h1", "h6", "h24"):
            c["vol"][k] += num(vol.get(k))
            c["txns"][k][0] += int(num((tx.get(k) or {}).get("buys")))
            c["txns"][k][1] += int(num((tx.get(k) or {}).get("sells")))
        liq = num((p.get("liquidity") or {}).get("usd"))
        if liq > 2e7 and num(vol.get("h24")) < liq * 0.005:    # huge "liquidity" nobody trades against: fake pool
            return c
        c["liq_total"] += liq
        created = num(p.get("pairCreatedAt")) / 1000 or None
        if created and (not c["created"] or created < c["created"]):
            c["created"] = created
        info = p.get("info") or {}
        add_links(c, info.get("websites"))
        add_links(c, info.get("socials"))
        c["boosts"] = max(c["boosts"], int(num((p.get("boosts") or {}).get("active"))))
        if liq > c["liq"]:
            pc = p.get("priceChange") or {}
            c.update(symbol=sym, name=(bt.get("name") or sym).strip(), pair_url=safe_url(p.get("url")) or f"https://dexscreener.com/{chain}/{addr}", dex=p.get("dexId"),
                     price=num(p.get("priceUsd")), mcap=num(p.get("marketCap")) or num(p.get("fdv")), liq=liq,
                     chg={k: num(pc.get(k)) for k in ("m5", "h1", "h6", "h24")}, has_data=True)
        return c
    except Exception as e:
        _mark("parse", False, f"pair: {e}")
        return None


# ------------------------------------------------------------------ fetchers (raw data only; run in parallel threads)
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
GTH = {"Accept": "application/json;version=20230302"}
CG = "https://api.coingecko.com/api/v3"
CB = "https://api.coinbase.com/api/v3/brokerage/market"


def f_ds_lists():
    out = {}
    for path, src in [("/token-profiles/latest/v1", "dex_profile"), ("/token-boosts/latest/v1", "dex_boost"),
                      ("/token-boosts/top/v1", "dex_boost_top"), ("/community-takeovers/latest/v1", "dex_cto")]:
        out[src] = fetch(DS + path, "DexScreener lists") or []
    return out


def f_ds_metas():
    j = fetch(DS + "/metas/trending/v1", "DexScreener narratives")
    metas = []
    for m in (j if isinstance(j, list) else [])[:30]:
        ch = m.get("marketCapChange") or {}
        metas.append({"name": m.get("name") or m.get("slug"), "slug": m.get("slug"), "desc": (m.get("description") or "")[:200],
                      "mcap": num(m.get("marketCap")), "liq": num(m.get("liquidity")), "vol": num(m.get("volume")),
                      "tokens": int(num(m.get("tokenCount"))), "chg": {k: num(ch.get(k)) for k in ("h1", "h6", "h24")}, "pairs": []})
    hot = sorted([m for m in metas if m["slug"]], key=lambda m: -(m["chg"]["h6"] + m["chg"]["h1"]))[:7]
    for m in hot:
        jj = fetch(DS + "/metas/meta/v1/" + urllib.parse.quote(m["slug"]), "DexScreener narratives")
        m["pairs"] = (jj or {}).get("pairs") or [] if isinstance(jj, dict) else []
    return metas


def f_ds_search(queries):
    out = []
    for q, must in queries:
        j = fetch(DS + "/latest/dex/search?q=" + urllib.parse.quote(q), "DexScreener search")
        out.append((q, must, (j or {}).get("pairs") or [] if isinstance(j, dict) else []))
    return out


def f_gt():
    urls = [("/networks/trending_pools?include=base_token&duration=1h&page=1", "gt_trending"),
            ("/networks/trending_pools?include=base_token&duration=1h&page=2", "gt_trending"),
            ("/networks/trending_pools?include=base_token&duration=6h&page=1", "gt_trending"),
            ("/networks/solana/trending_pools?include=base_token&duration=1h&page=1", "gt_trending"),
            ("/networks/base/trending_pools?include=base_token&duration=1h&page=1", "gt_trending"),
            ("/networks/new_pools?include=base_token&page=1", "gt_new")]
    return [(src, fetch(GT + u, "GeckoTerminal", headers=GTH)) for u, src in urls]


def f_cg():
    tr = fetch(CG + "/search/trending", "CoinGecko")
    mm = fetch(CG + "/coins/markets?vs_currency=usd&category=meme-token&order=market_cap_desc&per_page=200&page=1"
                    "&price_change_percentage=1h,24h,7d", "CoinGecko")
    return tr, mm


def f_cb():
    return fetch(CB + "/products?product_type=SPOT&limit=3000", "Coinbase", timeout=25)


BIZ_THREADS = []


def f_biz():
    docs = []
    cat = fetch("https://a.4cdn.org/biz/catalog.json", "4chan /biz/")
    threads = []
    for page in cat if isinstance(cat, list) else []:
        for t in page.get("threads", []):
            threads.append(t)
            texts = [(t.get("sub") or "") + " " + (t.get("com") or "")] + [r.get("com") or "" for r in t.get("last_replies") or []]
            for x in texts:
                docs.append(("biz", x, None, None, t.get("time")))
    # the crypto / meme-coin general threads hold most of the ticker talk: read the busiest few in full
    # (/smg/ on /biz/ is the STOCK market general, so it's skipped)
    def is_gen(t):
        head = (t.get("sub") or "") + " " + clean(t.get("com") or "")[:300]
        return (re.search(r"memecoin|meme coin|shitcoin|/cmg/|/mcg/|/acg/|/sol/|solana|pump\.?fun|degen|altcoin|crypto general|/crypto/|robinhood chain|base chain", head, re.I)
                and not re.search(r"/smg/|stock", head, re.I))
    gen = sorted([t for t in threads if is_gen(t)], key=lambda t: -int(t.get("replies") or 0))
    BIZ_THREADS[:] = [((t.get("sub") or clean(t.get("com") or ""))[:80], int(t.get("replies") or 0)) for t in gen[:8]]
    for t in gen[:5]:
        th = fetch(f"https://a.4cdn.org/biz/thread/{t.get('no')}.json", "4chan /biz/")
        for p in (th or {}).get("posts", [])[1:]:
            if NOW - num(p.get("time")) < 2 * 86400:
                docs.append(("biz", p.get("com") or "", None, None, p.get("time")))
    return docs


REDDIT_SUBS = ["CryptoMoonShots", "SatoshiStreetBets", "memecoins", "solana", "CryptoCurrency", "pumpfun", "SolanaMemeCoins", "memecoinmoonshots"]


def f_reddit():
    docs, fails = [], 0
    for s in REDDIT_SUBS:
        if fails >= 3:
            _mark("Reddit", False, "Reddit blocks GitHub's servers (HTTP 429/403); skipped the rest")
            break
        got = False
        for host in ("www.reddit.com", "old.reddit.com"):
            j = fetch(f"https://{host}/r/{s}/new.json?limit=100&raw_json=1", "Reddit", headers={"User-Agent": REDDIT_UA}, tries=1)
            kids = (((j or {}).get("data") or {}).get("children") or []) if isinstance(j, dict) else []
            for ch in kids:
                d = ch.get("data") or {}
                if NOW - num(d.get("created_utc")) > 2 * 86400:
                    continue
                w = 1 + min(num(d.get("score")), 200) / 100 + min(num(d.get("num_comments")), 200) / 100
                docs.append(("reddit", (d.get("title") or "") + " " + (d.get("selftext") or "")[:2000], d.get("title"),
                             "https://www.reddit.com" + (d.get("permalink") or ""), d.get("created_utc"), w))
            if kids:
                got = True
                break
        if not got:
            x = fetch(f"https://www.reddit.com/r/{s}/new/.rss?limit=100", "Reddit", kind="text", headers={"User-Agent": REDDIT_UA}, tries=1)
            items = parse_feed(x)
            for it in items:
                if it["t"] and NOW - it["t"] > 2 * 86400:
                    continue
                docs.append(("reddit", it["title"] + " " + it["desc"], it["title"], it["link"], it["t"]))
            got = bool(items)
        if not got:
            fails += 1
    return docs


NEWS_FEEDS = [("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"), ("https://cointelegraph.com/rss", "Cointelegraph"),
              ("https://decrypt.co/feed", "Decrypt"), ("https://www.theblock.co/rss.xml", "The Block"),
              ("https://news.google.com/rss/search?q=memecoin+OR+%22meme+coin%22+when:2d&hl=en-US&gl=US&ceid=US:en", "Google News"),
              ("https://news.google.com/rss/search?q=%22GTA+6%22+OR+%22GTA+VI%22+when:2d&hl=en-US&gl=US&ceid=US:en", "Google News (GTA VI)"),
              ("https://news.google.com/rss/search?q=crypto+token+viral+when:1d&hl=en-US&gl=US&ceid=US:en", "Google News")]


NO_MENTION_OUTLETS = {"Google News (GTA VI)"}


def f_news():
    docs = []
    for url, name in NEWS_FEEDS:
        for it in parse_feed(fetch(url, "News: " + name, kind="text")):
            if it["t"] and NOW - it["t"] > 3 * 86400:
                continue
            docs.append(("news", it["title"] + " " + it["desc"][:600], it["title"], it["link"], it["t"], name))
    return docs


def f_trends():
    items = parse_feed(fetch("https://trends.google.com/trending/rss?geo=US", "Google Trends", kind="text"))
    out = []
    for it in items[:25]:
        out.append({"term": it["title"], "traffic": it["traffic"], "news": it["extra"][:2]})
    return out


COMMON_WORDS = set("""what when where time today tonight game games news live score scores near team weather update death dies dead
died schedule season week year days night show video watch where how who will open start world state city county high school
football basketball baseball soccer hockey match final finals vote results""".split())


def traffic_n(s):
    m = re.match(r"([\d.,]+)\s*([KkMm]?)", s or "")
    if not m:
        return 0
    v = num(m.group(1))
    return v * {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1)


def f_cb_hourly(pids):
    out = {}
    end = int(NOW)
    for pid in pids:
        j = fetch(f"{CB}/products/{pid}/candles?granularity=ONE_HOUR&start={end - 27 * 3600}&end={end}", "Coinbase")
        bars = sorted((int(num(c.get("start"))), num(c.get("open")), num(c.get("close")), num(c.get("volume")))
                      for c in ((j or {}).get("candles") or []))
        if len(bars) >= 6:
            done = [b for b in bars if b[0] + 3600 <= end]
            if len(done) >= 5:
                last, prior = done[-1], done[-25:-1]
                avg = sum(b[3] for b in prior) / max(len(prior), 1)
                out[pid] = {"spike": last[3] / avg if avg > 0 else 0, "chg1h": (bars[-1][2] / last[1] - 1) * 100 if last[1] else 0}
    return out


# ------------------------------------------------------------------ security
def sec_rugcheck(c):
    j = fetch(f"https://api.rugcheck.xyz/v1/tokens/{c['address']}/report/summary", "RugCheck", tries=2)
    if not isinstance(j, dict):
        return None
    risks = [r for r in j.get("risks") or [] if isinstance(r, dict)]
    bad, warn = [], []
    for r in risks:
        nm, lvl = (r.get("name") or "").strip(), (r.get("level") or "").lower()
        low = nm.lower()
        if "mint authority" in low or "freeze authority" in low:
            bad.append(nm)
        elif lvl == "danger":
            (bad if any(w in low for w in ("honeypot", "copycat", "mutable balance", "transfer fee")) else warn).append(nm + (f" ({r.get('value')})" if r.get("value") else ""))
        elif lvl == "warn" and any(w in low for w in ("top 10", "single holder", "lp unlocked", "creator")):
            warn.append(nm + (f" ({r.get('value')})" if r.get("value") else ""))
    return {"by": "RugCheck", "bad": bad[:4], "warn": warn[:4], "score": j.get("score_normalised", j.get("score")),
            "lp_locked": j.get("lpLockedPct")}


def sec_goplus(c):
    ch, a = c["chain"], c["address"]
    if ch == "solana":
        j = fetch(f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={a}", "GoPlus", tries=2)
        r = ((j or {}).get("result") or {}).get(a) if isinstance(j, dict) else None
        if not r:
            return None
        bad, warn = [], []
        if str((r.get("mintable") or {}).get("status")) == "1":
            bad.append("Mint authority still enabled (more tokens can be printed)")
        if str((r.get("freezable") or {}).get("status")) == "1":
            bad.append("Freeze authority enabled (your tokens can be frozen)")
        if str((r.get("balance_mutable_authority") or {}).get("status")) == "1":
            bad.append("Balances can be changed by an authority")
        top = sum(num(h.get("percent")) for h in (r.get("holders") or [])[:10])
        if top > 0.5:
            warn.append(f"Top 10 wallets hold {top * 100:.0f}%")
        return {"by": "GoPlus", "bad": bad, "warn": warn}
    cid = EVM_ID.get(ch)
    if not cid:
        return None
    j = fetch(f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={a}", "GoPlus", tries=2)
    r = ((j or {}).get("result") or {}).get(a.lower()) if isinstance(j, dict) else None
    if not r:
        return None
    bad, warn = [], []
    if r.get("is_honeypot") == "1" or r.get("cannot_sell_all") == "1":
        bad.append("Honeypot: buyers can't sell")
    st, bt = num(r.get("sell_tax")), num(r.get("buy_tax"))
    if st > 0.15 or bt > 0.15:
        bad.append(f"Tax {bt * 100:.0f}% buy / {st * 100:.0f}% sell")
    elif st > 0.05 or bt > 0.05:
        warn.append(f"Tax {bt * 100:.0f}% buy / {st * 100:.0f}% sell")
    if r.get("owner_change_balance") == "1":
        bad.append("Owner can change balances")
    if r.get("hidden_owner") == "1":
        bad.append("Hidden owner")
    renounced = (r.get("owner_address") or "").lower() in ("", "0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead")
    if r.get("is_mintable") == "1" and not renounced:
        warn.append("Owner can mint more tokens")
    if r.get("transfer_pausable") == "1" and not renounced:
        warn.append("Trading can be paused")
    if r.get("is_blacklisted") == "1" and not renounced:
        warn.append("Has a blacklist function")
    if r.get("is_open_source") == "0":
        warn.append("Contract code not verified")
    top = 0.0
    for h in (r.get("holders") or [])[:12]:
        tagn = (h.get("tag") or "").lower()
        if str(h.get("is_locked")) == "1" or "burn" in tagn or "null" in tagn or "lp" in tagn or "pool" in tagn:
            continue
        top += num(h.get("percent"))
    if top > 0.5:
        warn.append(f"Top wallets hold {top * 100:.0f}%")
    return {"by": "GoPlus", "bad": bad[:4], "warn": warn[:4]}


def security(c, cache):
    e = cache.get(c["key"])
    if e and NOW - e.get("t", 0) < 6 * 3600:
        return e
    res = None
    if c["chain"] == "solana":
        res = sec_rugcheck(c) or sec_goplus(c)
    elif c["chain"] in EVM_ID:
        res = sec_goplus(c)
    if res is None:
        res = {"by": "", "bad": [], "warn": [], "verdict": "unchecked"}
    else:
        res["verdict"] = "danger" if res["bad"] else ("caution" if res["warn"] else "pass")
    res["t"] = NOW
    cache[c["key"]] = res
    return res


# ------------------------------------------------------------------ chatter
class Doc:
    __slots__ = ("src", "w", "low", "cash", "upper", "addrs", "title", "link", "t", "outlet")

    def __init__(self, src, text, title=None, link=None, t=None, w=1.0, outlet=None):
        tx = clean(text)[:6000]
        self.src, self.w, self.title, self.link, self.t, self.outlet = src, w, title, safe_url(link), num(t) or None, outlet
        self.low = tx.lower()
        self.cash = {m.lower() for m in CASH_RE.findall(tx)}
        self.upper = set(UP_RE.findall(tx))
        self.addrs = set(SOL_RE.findall(tx)) | {a.lower() for a in EVM_RE.findall(tx)}


def build_docs(raw_biz, raw_reddit, raw_news):
    docs = []
    for src, text, title, link, t in raw_biz:
        docs.append(Doc("biz", text, title, link, t))
    for r in raw_reddit:
        docs.append(Doc("reddit", r[1], r[2], r[3], r[4], r[5] if len(r) > 5 else 1.0))
    for r in raw_news:
        docs.append(Doc("news", r[1], r[2], r[3], r[4], 1.0, r[5]))
    return docs


def mentions(c, docs, idx):
    sym = c["symbol"] or ""
    s_low, s_up = sym.lower(), sym.upper()
    akey = c["address"] if c["chain"] in CASE_SENSITIVE_CHAINS else (c["address"] or "").lower()
    bare_ok = len(sym) >= 3 and s_up not in STOP and re.fullmatch(r"[A-Za-z0-9]+", sym or "-") is not None
    name = (c["name"] or "").lower().strip()
    name_ok = len(name) >= 5 and name.upper() not in STOP and name != s_low and not name.isdigit()
    name_re = re.compile(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])") if name_ok else None
    hits = set(idx["cash"].get(s_low, ())) | set(idx["addr"].get(akey, ()))
    if bare_ok:
        hits |= {i for i in idx["upper"].get(s_up, ()) if docs[i].src != "news"}
    if name_ok:
        for i, d in enumerate(docs):
            if name in d.low and name_re.search(d.low):
                hits.add(i)
    m = {"biz": 0, "reddit": 0, "news": 0, "w": 0.0, "heads": []}
    for i in hits:
        d = docs[i]
        if d.outlet in NO_MENTION_OUTLETS:    # event news: counts toward the catalyst, not toward a coin's chatter
            continue
        m[d.src] += 1
        m["w"] += d.w * SRC_W[d.src]
        if d.src == "news" and d.title and len(m["heads"]) < 3:
            m["heads"].append({"title": d.title[:160], "link": d.link, "outlet": d.outlet})
    m["n"] = m["biz"] + m["reddit"] + m["news"]
    m["w"] = round(m["w"], 2)
    return m


def theme_hits(text):
    return {k for k, rx in THEME_RE.items() if rx.search(text or "")}


def catalyst_for(theme, today):
    best = None
    for cat in CATALYSTS:
        if cat["theme"] != theme:
            continue
        d = (date.fromisoformat(cat["date"]) - today).days
        if 0 <= d <= 45 and (best is None or d < best[0]):
            best = (d, cat)
    return best


# ------------------------------------------------------------------ scoring
def age_h(c):
    return (NOW - c["created"]) / 3600 if c.get("created") else None


def flag_note(c):
    s, n = (c.get("symbol") or "").lower(), (c.get("name") or "").lower()
    for k, note in MANUAL_FLAGS.items():
        if k == s or k in n:
            return note
    return None


def score(c, ctx):
    R, W, pts = [], [], {}
    v, ch, tx = c["vol"], c["chg"], c["txns"]
    a = age_h(c)
    # 1. momentum (0-30): volume running ahead of its own 24h pace, price rising, still accelerating
    hours = clamp(a, 1, 24) if a else 24
    pace = v["h24"] / hours if v["h24"] > 0 else 0
    va = v["h1"] / pace if pace > 0 else 0
    m = clamp(math.log2(va) * 8, 0, 16) if va > 1 else 0
    m *= clamp(v["h1"] / 25000, 0.2, 1)          # a spike on $3k of volume is noise
    if ch["h1"] < 0:
        m *= 0.5                                 # heavy volume while the price falls is selling, not a breakout
    if va >= 1.8 and v["h1"] >= 10000:
        R.append(f"volume {va:.1f}x its normal pace" + (" (but price falling)" if ch["h1"] < 0 else ""))
    if ch["h1"] > 0:
        m += clamp(ch["h1"] / 5, 0, 8)
    if ch["m5"] > 0 and v["m5"] * 12 > v["h1"] * 1.2 and v["m5"] > 1000:
        m += 3
        R.append("still accelerating (last 5 min)")
    if ch["h6"] > 10:
        m += 3
    pts["momentum"] = min(30, m)
    # 2. buyers (0-12)
    b1, s1 = tx["h1"]
    b6, s6 = tx["h6"]
    bp = 0
    if b1 + s1 >= 20:
        r1 = b1 / (b1 + s1)
        bp += clamp((r1 - 0.5) * 40, 0, 8)
        if r1 >= 0.6:
            R.append(f"{r1 * 100:.0f}% of trades are buys (1h)")
    if b6 + s6 >= 50:
        bp += clamp((b6 / (b6 + s6) - 0.5) * 20, 0, 4)
    g = c.get("gt") or {}
    ub, us = g.get("buyers_h1", 0), g.get("sellers_h1", 0)
    if ub + us >= 20 and ub / (ub + us) >= 0.6:
        bp += 2
        R.append(f"{int(ub)} unique buyers vs {int(us)} sellers (1h)")
    pts["buyers"] = min(12, bp)
    # 3. chatter (0-25)
    mt = c["ment"]
    cp = 6 * math.log2(1 + mt["w"]) if mt["w"] else 0
    parts = [f"{mt[k]} on {lbl}" for k, lbl in (("biz", "/biz/"), ("reddit", "Reddit"), ("news", "news")) if mt[k]]
    if parts:
        R.append("mentioned " + ", ".join(parts))
    if c.get("cg_rank"):
        cp += 8
        R.append(f"#{c['cg_rank']} trending on CoinGecko")
    if "gt_trending" in c["src"]:
        cp += 3
        R.append("trending on GeckoTerminal")
    if "dex_cto" in c["src"]:
        cp += 3
        R.append("community takeover")
    if c["boosts"] or "dex_boost" in c["src"] or "dex_boost_top" in c["src"]:
        cp += 2
        R.append("paid DexScreener boosts")
    if "dex_profile" in c["src"]:
        cp += 1
    prev = ctx["prev_ment"].get(c["key"])
    if mt["n"] >= 3 and (prev is None or mt["n"] >= 2 * prev + 1):
        cp += 5
        R.append("chatter rising fast")
    pts["chatter"] = min(25, cp)
    # 4. narrative (0-15)
    npts = 0
    for th in sorted(c["themes"]):
        cat = catalyst_for(th, ctx["today"])
        if cat:
            npts = max(npts, 3 + 10 * (1 - cat[0] / 60))
            R.append(f"{THEMES[th]['label']}: {cat[1]['event']} in {cat[0]} days")
    if c["metas"]:
        best = max(c["metas"], key=lambda mm: mm[1])
        npts += clamp(best[1] / 10, 1, 6)
        R.append(f"in hot DexScreener narrative '{best[0]}'")
    mn = next((v for k, v in MANUAL_NOTES.items() if k == (c.get("symbol") or "").lower() or (c.get("name") or "").lower() in (k, k + " coin")), None)
    if mn:
        npts += 5
        R.insert(0, mn)
    if c.get("culture"):
        npts += 6
        R.append(f"named after a Google trending search: '{c['culture']}'")
    pts["narrative"] = min(15, npts)
    # 5. room to run (0-12) and liquidity (0-6)
    mc = c["mcap"]
    room = 0 if mc <= 0 else 6 if mc < 1e5 else 12 if mc < 1e6 else 10 if mc < 1e7 else 7 if mc < 5e7 else 4 if mc < 2.5e8 else 2 if mc < 1e9 else 1
    pts["room"] = room
    lq = max(c["liq"], 0)
    lp = (4 if lq >= 1e5 else 2 if lq >= 4e4 else 0) + (2 if mc > 0 and 0.05 <= lq / mc <= 0.6 else 0)
    pts["liquidity"] = lp
    # penalties
    pen = 0
    if lq < 25000:
        pen += 15
        W.append(f"thin liquidity (${lq:,.0f}): hard to sell")
    elif mc > 0 and lq / mc < 0.03:
        pen += 5
        W.append("liquidity under 3% of market cap")
    if a is not None and a < 1:
        pen += 10
        W.append("launched under an hour ago (most rugs happen here)")
    elif a is not None and a < 6:
        pen += 4
        W.append(f"only {a:.0f}h old")
    if ch["h24"] > 1000:
        pen += 12
        W.append(f"already up {ch['h24']:,.0f}% in 24h (late)")
    elif ch["h24"] > 300:
        pen += 6
        W.append(f"already up {ch['h24']:,.0f}% in 24h")
    if b1 > 60 and s1 == 0:
        pen += 15
        W.append("no sells in the last hour: possible honeypot")
    if ch["h1"] < -15:
        pen += 5
        W.append(f"dumping now ({ch['h1']:.0f}% in 1h)")
    sec = c.get("sec") or {}
    if sec.get("verdict") == "danger":
        pen += 30
        W.extend(sec["bad"])
    elif sec.get("verdict") == "caution":
        pen += 6
        W.extend(sec["warn"][:2])
    elif sec.get("verdict") == "unchecked" and c["chain"] not in ("solana",) and c["chain"] not in EVM_ID:
        pen += 3
    note = flag_note(c)
    if note:
        pen += 25
        W.insert(0, note)
    if c.get("copy_note"):
        W.append(c["copy_note"])
        if c.get("copy_younger"):
            pen += 8           # a later copy of a coin that's already running is the classic trap
    total = clamp(sum(pts.values()) - pen, 0, 100)
    c["score"], c["pts"], c["pen"], c["why"], c["warn"] = round(total), {k: round(x, 1) for k, x in pts.items()}, pen, R, W
    return total


def cb_heat(e, ctx):
    R, s = [], 0.0
    if e["spike"] > 1:
        s += clamp(math.log2(e["spike"]) * 15, 0, 30)
        if e["spike"] >= 2:
            R.append(f"last hour volume {e['spike']:.1f}x normal")
    if e["volchg24"] > 0:
        s += clamp(e["volchg24"] / 10, 0, 15)
        if e["volchg24"] >= 50:
            R.append(f"24h volume +{e['volchg24']:.0f}% vs prior day")
    if e["chg1h"] > 0:
        s += clamp(e["chg1h"] * 3, 0, 15)
    if e["chg24"] > 0:
        s += clamp(e["chg24"] / 2, 0, 10)
    mt = e["ment"]
    if mt["w"]:
        s += clamp(6 * math.log2(1 + mt["w"]), 0, 20)
        R.append(f"{mt['n']} mentions in chatter")
    if e.get("cg_rank"):
        s += 10
        R.append(f"#{e['cg_rank']} trending on CoinGecko")
    for th in sorted(e["themes"]):
        cat = catalyst_for(th, ctx["today"])
        if cat:
            s += clamp(3 + 7 * (1 - cat[0] / 60), 0, 10)
            R.append(f"{cat[1]['event']} in {cat[0]} days")
            break
    if e["new"]:
        s += 10
        R.append("newly listed on Coinbase")
    if e["chg7d"] > 100:
        s -= 5
    e["heat"], e["why"] = round(clamp(s, 0, 100)), R
    return e["heat"]


# ------------------------------------------------------------------ paper tickets
def ticket_value(t, price):
    if not price or not t.get("entry"):
        return 0.0
    return TICKET_USD * (price / t["entry"]) * (1 - COST_EACH_WAY) ** 2


def ds_prices(keys):
    """Current price + liquidity for DEX tickets, batched 30 per call."""
    by_chain, out = {}, {}
    for k in keys:
        ch, a = k.split(":", 1)
        by_chain.setdefault(ch, []).append(a)
    for ch, addrs in by_chain.items():
        for i in range(0, len(addrs), 30):
            j = fetch(f"{DS}/tokens/v1/{ch}/" + ",".join(addrs[i:i + 30]), "DexScreener prices")
            for p in j if isinstance(j, list) else []:
                k = ckey(p.get("chainId"), (p.get("baseToken") or {}).get("address"))
                liq = num((p.get("liquidity") or {}).get("usd"))
                if k in keys and liq >= out.get(k, (0, -1))[1]:
                    out[k] = (num(p.get("priceUsd")), liq)
    return out


def update_ledger(ledger, ranked, cbm, cbprods, claude_picks):
    tickets = ledger.setdefault("tickets", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Claude's hand picks (docs/meme/claude_picks.json) become tickets the first time the bot sees them
    have = {(t["key"], t["by"]) for t in tickets}
    for p in claude_picks:
        k = ckey(p["chain"], p["address"]) if p.get("address") else "cb:" + p.get("cb", "").upper()
        if (k, "claude") in have:
            continue
        c = C.get(k)
        price = p.get("entry") or (c or {}).get("price") or (cbprods.get(k[3:], {}).get("price") if k.startswith("cb:") else 0)
        if price:
            tickets.append({"key": k, "by": "claude", "symbol": p.get("symbol"), "name": p.get("name"), "chain": p.get("chain") or "coinbase",
                            "url": p.get("url") or (c or {}).get("pair_url"), "t": p.get("t") or NOW, "entry": price, "peak": price, "last": price,
                            "last_t": NOW, "status": "open", "score": (c or {}).get("score"), "why": p.get("thesis", "")})
    # mark open tickets to market
    open_t = [t for t in tickets if t["status"] == "open"]
    dex_keys = {t["key"] for t in open_t if not t["key"].startswith("cb:")}
    known = {k: (C[k]["price"], C[k]["liq"]) for k in dex_keys if k in C and C[k].get("has_data")}
    missing = dex_keys - set(known)
    if missing:
        known.update(ds_prices(missing))
    for t in open_t:
        if t["key"].startswith("cb:"):
            pr = cbprods.get(t["key"][3:])
            price, liq = (pr["price"], 1e9) if pr and pr["price"] else (None, None)
        else:
            price, liq = known.get(t["key"], (None, None))
        if price:
            t["last"], t["last_t"], t["peak"] = price, NOW, max(t.get("peak", price), price)
            if liq is not None and liq < 1000:
                t["status"], t["exit"], t["exit_t"], t["note"] = "rugged", 0.0, NOW, "liquidity pulled"
        elif NOW - t.get("last_t", t["t"]) > 86400:
            t["status"], t["exit"], t["exit_t"], t["note"] = "dead", 0.0, NOW, "no price for 24h (delisted or rugged)"
        if t["status"] == "open" and NOW - t["t"] > TICKET_DAYS * 86400:
            t["status"], t["exit"], t["exit_t"] = "closed", t["last"], NOW
    # new bot tickets: strongest DEX picks, max 5 a day, never the same coin twice in 14 days
    recent = {t["key"] for t in tickets if NOW - t["t"] < TICKET_DAYS * 86400}
    n_dex = sum(1 for t in tickets if t["by"] == "bot" and not t["key"].startswith("cb:") and t.get("day") == today)
    for c in ranked:
        if n_dex >= BOT_TICKETS_PER_DAY or c["score"] < PICK_SCORE:
            break
        a = age_h(c)
        sec = (c.get("sec") or {}).get("verdict")
        if (c["key"] in recent or sec == "danger" or flag_note(c) or c["liq"] < 40000 or (a is not None and a < 3)
                or c["chg"]["h24"] > 500 or not c["price"]):
            continue
        tickets.append({"key": c["key"], "by": "bot", "day": today, "symbol": c["symbol"], "name": c["name"], "chain": c["chain"],
                        "url": c.get("pair_url"), "t": NOW, "entry": c["price"], "peak": c["price"], "last": c["price"], "last_t": NOW,
                        "status": "open", "score": c["score"], "why": "; ".join(c["why"][:4]), "mcap": c["mcap"]})
        recent.add(c["key"])
        n_dex += 1
    n_cb = sum(1 for t in tickets if t["by"] == "bot" and t["key"].startswith("cb:") and t.get("day") == today)
    for e in cbm:
        if n_cb >= CB_TICKETS_PER_DAY or e["heat"] < CB_PICK_HEAT:
            break
        k = "cb:" + e["symbol"]
        if k in recent or not e["price"]:
            continue
        tickets.append({"key": k, "by": "bot", "day": today, "symbol": e["symbol"], "name": e["name"], "chain": "coinbase",
                        "url": f"https://www.coinbase.com/advanced-trade/spot/{e['pid']}", "t": NOW, "entry": e["price"], "peak": e["price"],
                        "last": e["price"], "last_t": NOW, "status": "open", "score": e["heat"], "why": "; ".join(e["why"][:4]), "mcap": e["mcap"]})
        recent.add(k)
        n_cb += 1
    return ledger


def ledger_summary(tickets, by):
    ts = [t for t in tickets if t["by"] == by]
    if not ts:
        return None
    vals = [ticket_value(t, t["last"] if t["status"] == "open" else t.get("exit", 0)) for t in ts]
    peaks = [t["peak"] / t["entry"] if t.get("entry") else 0 for t in ts]
    return {"n": len(ts), "open": sum(t["status"] == "open" for t in ts), "paid": TICKET_USD * len(ts), "value": sum(vals),
            "best": max(peaks), "x2": sum(p >= 2 for p in peaks), "x5": sum(p >= 5 for p in peaks), "x10": sum(p >= 10 for p in peaks),
            "wiped": sum(v <= TICKET_USD * 0.2 for v in vals)}


# ------------------------------------------------------------------ html
def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def usd(x):
    x = num(x)
    if x <= 0:
        return "–"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if x >= div:
            return f"${x / div:.1f}{suf}" if x / div < 100 else f"${x / div:.0f}{suf}"
    return f"${x:,.0f}"


def price_s(p):
    p = num(p)
    if p <= 0:
        return "–"
    if p >= 1:
        return f"${p:,.2f}" if p < 1e5 else f"${p:,.0f}"
    s = f"{p:.12f}"
    lead = len(s.split(".")[1]) - len(s.split(".")[1].lstrip("0"))
    return f"${p:.{lead + 3}f}".rstrip("0")


def pct(x, bold=False):
    x = num(x)
    col = "#5cc98a" if x > 0 else "#ef7a79" if x < 0 else "#9aa6a0"
    s = f"{x:+.0f}%" if abs(x) >= 100 else f"{x:+.1f}%"
    return f"<span style='color:{col}'>{'<b>' if bold else ''}{s}{'</b>' if bold else ''}</span>"


def age_s(c):
    a = age_h(c)
    if a is None:
        return "age ?"
    if a < 1:
        return f"{a * 60:.0f}m old"
    if a < 48:
        return f"{a:.0f}h old"
    if a < 24 * 60:
        return f"{a / 24:.0f}d old"
    if a < 24 * 365:
        return f"{a / 24 / 30:.0f}mo old"
    return f"{a / 24 / 365:.1f}y old"


def sc_cls(s):
    return "hi" if s >= 70 else "mid" if s >= 55 else "lo"


def sec_pill(c):
    v = (c.get("sec") or {}).get("verdict")
    by = (c.get("sec") or {}).get("by") or ""
    if v == "pass":
        return f"<span class='pill ok' title='{esc(by)}'>safety ok</span>"
    if v == "caution":
        return f"<span class='pill close' title='{esc(by)}'>caution</span>"
    if v == "danger":
        return f"<span class='pill bad' title='{esc(by)}'>danger</span>"
    return "<span class=pill>not checked</span>"


CSS = """body{font:15px/1.5 system-ui,sans-serif;background:#111514;color:#eef3f1;max-width:980px;margin:0 auto;padding:24px 16px}
h1{font-size:26px;margin:0} h2{font-size:17px;margin:30px 0 8px} .muted{color:#9aa6a0} a{color:#8fc7ff}
table{border-collapse:collapse;width:100%;font-size:14px} td,th{padding:6px 8px;border-bottom:1px solid #2a312e;text-align:left;vertical-align:top}
th{color:#86918c;font-size:12px;text-transform:uppercase} .n{text-align:right;font-family:ui-monospace,monospace;white-space:nowrap} .wrap{overflow-x:auto}
.pill{display:inline-block;font:600 11px/1 ui-monospace,monospace;padding:3px 6px;border-radius:4px;background:#2a312e;color:#cfd8d4;white-space:nowrap}
.pill.ok{background:#1c3b2c;color:#79d9a3}.pill.close{background:#3f3219;color:#f0c374}.pill.bad{background:#40211f;color:#f39a93}
.xnav{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 10px;font-size:13px}.xnav a{color:#eef3f1;text-decoration:none;padding:4px 10px;border-radius:14px;border:1px solid #2a312e}
.xnav a.on{background:#eef3f1;color:#111514;font-weight:600}
.warnbox{border:1px solid #5a4421;background:#211b10;border-radius:8px;padding:10px 14px;margin:12px 0;color:#f3dfb8;font-size:14px}
.card{border:1px solid #2a312e;border-radius:10px;padding:10px 12px;margin:8px 0}
.r1{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}.rank{color:#86918c;font:600 13px ui-monospace,monospace;min-width:18px}
.sym{font-weight:700;font-size:16px;color:#eef3f1;text-decoration:none}.sym:hover{text-decoration:underline}
.score{margin-left:auto;font:700 15px ui-monospace,monospace;padding:2px 8px;border-radius:6px;background:#2a312e}
.score.hi{background:#1c3b2c;color:#79d9a3}.score.mid{background:#3f3219;color:#f0c374}.score.lo{color:#9aa6a0}
.met{font-size:13px;color:#cfd8d4;margin:4px 0 6px;font-family:ui-monospace,monospace}
.chips{display:flex;flex-wrap:wrap;gap:5px}.chip{font-size:12px;padding:2px 7px;border-radius:10px;background:#1b2421;color:#b9e6cd;border:1px solid #24352d}
.chip.w{background:#2a2114;color:#f0c374;border-color:#3f3219}.chip.r{background:#2c1716;color:#f39a93;border-color:#40211f}
details{margin:14px 0}summary{cursor:pointer;color:#cfd8d4} .small{font-size:13px}
.show-s{display:none}
@media(max-width:600px){body{padding:16px 12px}td,th{padding:5px 6px}.hide-s{display:none}.show-s{display:inline}}"""


def xnav():
    return (f'<div class=xnav><a href="{SITE}crypto-paper-bot/">Crypto bot</a><a href="{SITE}stock-paper-bot/">Stock bot</a>'
            f'<a class=on href="{SITE}crypto-paper-bot/meme/">Meme scanner</a></div>')


def is_bad(c):
    return (c.get("sec") or {}).get("verdict") == "danger" or bool(flag_note(c))


def card(i, c):
    chain = CHAIN_LABEL.get(c["chain"], c["chain"].title())
    link = c.get("pair_url") or ""
    sym = f"<a class=sym href='{esc(link)}' target=_blank rel=noopener>${esc(c['symbol'])}</a>" if link else f"<span class=sym>${esc(c['symbol'])}</span>"
    vt = c["txns"]["h1"]
    buys = f"buys {vt[0] / (vt[0] + vt[1]) * 100:.0f}%" if sum(vt) >= 20 else ""
    met = " · ".join(x for x in [f"mcap {usd(c['mcap'])}", f"liq {usd(c['liq'])}", f"1h {pct(c['chg']['h1'])}", f"24h {pct(c['chg']['h24'])}",
                                  f"vol 1h {usd(c['vol']['h1'])}", buys] if x)
    chips = "".join(f"<span class=chip>{esc(r)}</span>" for r in c["why"][:5])
    chips += "".join(f"<span class='chip {'r' if (c.get('sec') or {}).get('verdict') == 'danger' or flag_note(c) else 'w'}'>{esc(w)}</span>" for w in c["warn"][:4])
    trend = ""
    if c.get("score_6h") is not None:
        d = c["score"] - c["score_6h"]
        trend = f"<span class=muted style='font-size:12px'>{'▲' if d > 0 else '▼' if d < 0 else '•'} {abs(d)} vs 6h ago</span>"
    cb = " <span class='pill ok'>on Coinbase</span>" if c.get("on_cb") else ""
    return (f"<div class=card><div class=r1><span class=rank>{i}</span>{sym}<span class=muted>{esc(c['name'][:40])} · {esc(chain)} · {age_s(c)}</span>"
            f"{cb} {sec_pill(c)}{trend}<span class='score {sc_cls(c['score'])}'>{c['score']}</span></div>"
            f"<div class=met>{met}</div><div class=chips>{chips}</div></div>")


def write_page(ctx):
    top, cbm, metas, trends, chat, ledger, flagged, cats, cgcats = (ctx[k] for k in ("top", "cbm", "metas", "trends", "chat", "ledger", "flagged", "catalysts", "cg_categories"))
    upd = datetime.fromtimestamp(NOW, ET_TZ).strftime("%Y-%m-%d %I:%M %p ET")
    h = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<meta http-equiv=refresh content=180><title>Meme Scanner</title><style>{CSS}</style>", xnav(), "<h1>Meme Scanner</h1>",
         f"<p class=muted>RESEARCH + PAPER ONLY: no orders, no wallet. Updated {upd}. Scanned {ctx['n_scanned']} coins from "
         f"{ctx['n_sources_ok']} of {ctx['n_sources']} sources in {ctx['secs']:.0f}s. Rescans about every 15 minutes.</p>",
         "<div class=warnbox><b>Read this first.</b> Most meme coins go to zero, and plenty are built to rug the people who buy late. "
         "A high score means unusual buying, chatter and narrative <i>right now</i>. It does not mean safe. "
         "Only put in money you'd be fine losing in full, check the safety pill, and never buy a coin from a link someone DMs you. "
         "The paper tickets at the bottom show, over the coming weeks, whether these scores actually pick winners.</div>"]
    # top picks
    h.append("<h2>Top picks right now</h2><p class='muted small'>Small and mid caps found on decentralized exchanges, ranked by the score (0-100). "
             "Tap a ticker to open its chart on DexScreener. Green chips are reasons for the score; amber/red chips are warnings.</p>")
    if top and top[0]["score"] < 45:
        h.append("<p class=small><b>Nothing strong right now.</b> These are the best of a weak scan; usually that means wait.</p>")
    if top:
        h.extend(card(i + 1, c) for i, c in enumerate(top))
    else:
        h.append("<p class=muted>No coin scored high enough this scan.</p>")
    # coinbase
    h.append("<h2>Coinbase-listed memes heating up</h2><p class='muted small'>Easier to buy and much less rug risk, but also much less upside: "
             "these are already big. 'Vol now' compares the last full hour's Coinbase volume with the 24 hours before it.</p>")
    if cbm:
        h.append("<div class=wrap><table><tr><th>Coin</th><th class='n hide-s'>Price</th><th class=n>1h</th><th class=n>24h</th><th class='n hide-s'>7d</th>"
                 "<th class=n>Vol now</th><th class='n hide-s'>Mcap</th><th class=n>Heat</th><th class=hide-s>Why</th></tr>")
        for e in cbm[:15]:
            newpill = " <span class='pill ok'>new</span>" if e["new"] else ""
            h.append(f"<tr><td><a href='https://www.coinbase.com/advanced-trade/spot/{esc(e['pid'])}' target=_blank rel=noopener><b>{esc(e['symbol'])}</b></a>"
                     f"{newpill}<div class='muted small'>{esc(e['name'][:24])}</div></td>"
                     f"<td class='n hide-s'>{price_s(e['price'])}</td><td class=n>{pct(e['chg1h'])}</td><td class=n>{pct(e['chg24'])}</td><td class='n hide-s'>{pct(e['chg7d'])}</td>"
                     f"<td class=n>{e['spike']:.1f}x</td><td class='n hide-s'>{usd(e['mcap'])}</td><td class=n><span class='score {sc_cls(e['heat'])}' style='margin:0'>{e['heat']}</span></td>"
                     f"<td class='hide-s small'>{esc('; '.join(e['why'][:3]))}</td></tr>")
        h.append("</table></div>")
    else:
        h.append("<p class=muted>Coinbase or CoinGecko didn't answer this scan.</p>")
    if ctx["cb_new"]:
        h.append("<p class=small><b>Recently listed on Coinbase:</b> " + ", ".join(
            f"{esc(s)} <span class=muted>({esc(d)})</span>" for s, d in ctx["cb_new"][:12]) + "</p>")
    # narratives
    h.append("<h2>Narratives and catalysts</h2><p class='muted small'>Meme coins tied to an event tend to run up <i>into</i> it and dump after "
             "(\"buy the rumor, sell the news\"). Headlines count crypto and Google News stories from the last 2 days.</p>")
    h.append("<div class=wrap><table><tr><th>Date</th><th>Event</th><th class=n>Days</th><th class=n>Headlines</th><th>Coins riding it</th></tr>")
    for cat in cats:
        coins = " ".join(f"<a class='chip{' r' if is_bad(c) else ''}' href='{esc(c.get('pair_url') or '')}' target=_blank rel=noopener>${esc(c['symbol'])} "
                         f"{'red flag' if is_bad(c) else c['score']}</a>" for c in cat["coins"][:6]) or "<span class=muted>none found yet</span>"
        h.append(f"<tr><td class=n>{esc(cat['date'][5:])}</td><td>{esc(cat['event'])}</td><td class=n>{cat['days']}</td><td class=n>{cat['heads']}</td><td>{coins}</td></tr>")
    h.append("</table></div>")
    if metas:
        h.append("<p class=small style='margin-top:14px'><b>Hottest narratives on DexScreener</b> (market cap change of all coins in the narrative)</p>"
                 "<div class=wrap><table><tr><th>Narrative</th><th class=n>Mcap</th><th class=n>1h</th><th class=n>6h</th><th class=n>24h</th><th class='n hide-s'>Coins</th></tr>")
        for m in metas[:10]:
            h.append(f"<tr><td><a href='https://dexscreener.com/metas/{esc(m['slug'])}' target=_blank rel=noopener>{esc(m['name'])}</a></td><td class=n>{usd(m['mcap'])}</td>"
                     f"<td class=n>{pct(m['chg']['h1'])}</td><td class=n>{pct(m['chg']['h6'])}</td><td class=n>{pct(m['chg']['h24'])}</td><td class='n hide-s'>{m['tokens']}</td></tr>")
        h.append("</table></div>")
    if cgcats:
        h.append("<p class=small><b>Trending categories on CoinGecko:</b> " + ", ".join(
            f"{esc(x['name'])} {pct(x['chg24'])}" for x in cgcats[:8]) + "</p>")
    # culture
    h.append("<h2>Trending in culture right now</h2><p class='muted small'>Top US Google searches. When something goes viral, someone launches a coin named "
             "after it within hours; the first one to get real volume sometimes runs. Most die within a day.</p>")
    if trends:
        hit = [t for t in trends if t["coins"]]
        if hit:
            h.append("<div class=wrap><table><tr><th>Search</th><th class=n>Searches</th><th>Coins named after it (launched this week)</th></tr>")
            for t in hit[:8]:
                coins = " ".join(f"<a class=chip href='{esc(c.get('pair_url') or '')}' target=_blank rel=noopener>${esc(c['symbol'])} · {usd(c['mcap'])} · {age_s(c)}</a>" for c in t["coins"][:4])
                h.append(f"<tr><td>{esc(t['term'])}</td><td class=n>{esc(t['traffic'])}</td><td>{coins}</td></tr>")
            h.append("</table></div>")
        rest = [t for t in trends if not t["coins"]][:10]
        if rest:
            h.append("<p class=small>" + ("Other trending searches" if hit else "No new coins named after today's top searches") + ": "
                     + ", ".join(f"{esc(t['term'])} <span class=muted>({esc(t['traffic'])})</span>" for t in rest) + "</p>")
    else:
        h.append("<p class=muted>Google Trends didn't answer this scan.</p>")
    # chatter
    h.append("<h2>What people are talking about</h2><p class='muted small'>Most-mentioned tickers in the last ~2 days of /biz/, Reddit and news. "
             "'Before' is the average over earlier scans in the last 6 hours.</p>")
    if chat:
        h.append("<div class=wrap><table><tr><th>Ticker</th><th class=n>/biz/</th><th class=n>Reddit</th><th class='n hide-s'>News</th><th class=n>Total</th><th class='n hide-s'>Before</th><th>Coin</th></tr>")
        for r in chat[:15]:
            c = r.get("c")
            coin = f"<a href='{esc(c.get('pair_url') or '')}' target=_blank rel=noopener>{esc(c['name'][:22])}</a> <span class='score {sc_cls(c['score'])}' style='margin:0;font-size:12px'>{c['score']}</span>" if c else "<span class=muted>not found on a DEX</span>"
            before = f"{r['before']:.0f}" if r.get("before") is not None else "–"
            h.append(f"<tr><td><b>${esc(r['sym'])}</b></td><td class=n>{r['biz']}</td><td class=n>{r['reddit']}</td><td class='n hide-s'>{r['news']}</td>"
                     f"<td class=n><b>{r['n']}</b></td><td class='n hide-s'>{before}</td><td>{coin}</td></tr>")
        h.append("</table></div>")
    # ledger
    tickets = ledger.get("tickets", [])
    h.append(f"<h2>Paper tickets (${TICKET_USD:.0f} each)</h2><p class='muted small'>The bot paper-buys ${TICKET_USD:.0f} of up to {BOT_TICKETS_PER_DAY} DEX coins a day "
             f"that score {PICK_SCORE}+ and pass the safety filters (plus up to {CB_TICKETS_PER_DAY} Coinbase memes with heat {CB_PICK_HEAT}+), holds each for {TICKET_DAYS} days, "
             f"and charges {COST_EACH_WAY * 100:.0f}% each way for fees and slippage. 'Peak' is the best multiple reached, the most you could have sold for.</p>")
    rows = [(lbl, ledger_summary(tickets, by)) for by, lbl in (("bot", "Bot picks"), ("claude", "Claude's picks"))]
    rows = [(l, s) for l, s in rows if s]
    if rows:
        h.append("<div class=wrap><table><tr><th></th><th class=n>Tickets</th><th class='n hide-s'>Paid</th><th class=n>Worth</th><th class=n>Return</th>"
                 "<th class='n hide-s'>Best peak</th><th class=n>Hit 2x</th><th class='n hide-s'>Hit 5x</th><th class='n hide-s'>Hit 10x</th><th class='n hide-s'>Lost 80%+</th></tr>")
        for lbl, s in rows:
            h.append(f"<tr><td style='white-space:nowrap'><b>{lbl}</b></td><td class=n>{s['n']}<span class=hide-s> ({s['open']} open)</span></td><td class='n hide-s'>${s['paid']:,.0f}</td><td class=n>${s['value']:,.0f}</td>"
                     f"<td class=n>{pct((s['value'] / s['paid'] - 1) * 100, True)}</td><td class='n hide-s'>{s['best']:.1f}x</td><td class=n>{s['x2']}</td>"
                     f"<td class='n hide-s'>{s['x5']}</td><td class='n hide-s'>{s['x10']}</td><td class='n hide-s'>{s['wiped']}</td></tr>")
        h.append("</table></div>")
        h.append("<div class=wrap style='margin-top:10px'><table><tr><th>Bought</th><th>Coin</th><th class='n hide-s'>Entry</th><th class=n>Now</th><th class=n>Peak</th>"
                 "<th class=n>Worth</th><th class=hide-s>Status</th><th class=hide-s>Why it was picked</th></tr>")
        order = sorted(tickets, key=lambda t: (t["status"] != "open", -t["t"]))
        for t in order[:60]:
            price = t["last"] if t["status"] == "open" else t.get("exit", 0)
            val = ticket_value(t, price)
            mult = price / t["entry"] if t["entry"] else 0
            st = {"open": "<span class='pill ok'>open</span>", "closed": "<span class=pill>closed</span>"}.get(t["status"], f"<span class='pill bad'>{esc(t['status'])}</span>")
            who = " <span class=pill>Claude</span>" if t["by"] == "claude" else ""
            link = safe_url(t.get("url"))
            name = f"<a href='{esc(link)}' target=_blank rel=noopener><b>${esc(t['symbol'])}</b></a>" if link else f"<b>${esc(t['symbol'])}</b>"
            dead = f" <span class='pill bad'>{esc(t['status'])}</span>" if t["status"] in ("rugged", "dead") else ""
            h.append(f"<tr><td class=n>{datetime.fromtimestamp(t['t'], ET_TZ).strftime('%b %d')}<span class=hide-s> {datetime.fromtimestamp(t['t'], ET_TZ).strftime('%H:%M')}</span></td>"
                     f"<td>{name}{who}<span class=show-s>{dead}</span></td><td class='n hide-s'>{price_s(t['entry'])}</td>"
                     f"<td class=n><span class=hide-s>{price_s(price)} </span>{mult:.2f}x</td><td class=n>{t['peak'] / t['entry']:.2f}x</td><td class=n>${val:,.0f}</td>"
                     f"<td class=hide-s>{st}</td><td class='hide-s small muted'>{esc((t.get('why') or '')[:160])}</td></tr>")
        h.append("</table></div>")
    else:
        h.append("<p class=muted>No tickets yet. The first ones are bought when a coin clears the bar.</p>")
    # red flags
    if flagged:
        h.append("<h2>Red flags (skipped)</h2><p class='muted small'>Coins that had buying or chatter but failed a safety check. This is the stuff that turns $100 into $0.</p>"
                 "<div class=wrap><table><tr><th>Coin</th><th class=n>Mcap</th><th class=n>24h</th><th>Problem</th></tr>")
        seen = {}
        for c in flagged:
            seen[c["symbol"].upper()] = seen.get(c["symbol"].upper(), 0) + 1
        shown = set()
        for c in flagged:
            if c["symbol"].upper() in shown or len(shown) >= 12:
                continue
            shown.add(c["symbol"].upper())
            dup = seen[c["symbol"].upper()] - 1
            link = c.get("pair_url") or ""
            probs = flag_note(c) or "; ".join(((c.get("sec") or {}).get("bad") or [])[:3]) or "; ".join(c["warn"][:2])
            dups = f"<div class='muted small'>+{dup} copycat{'s' if dup > 1 else ''} with the same name</div>" if dup else ""
            h.append(f"<tr><td><a href='{esc(link)}' target=_blank rel=noopener><b>${esc(c['symbol'])}</b></a> <span class='muted small'>{esc(CHAIN_LABEL.get(c['chain'], c['chain']))}</span>{dups}</td>"
                     f"<td class=n>{usd(c['mcap'])}</td><td class=n>{pct(c['chg']['h24'])}</td><td class=small>{esc(probs)}</td></tr>")
        h.append("</table></div>")
    # how it works + sources
    h.append("<details><summary>How the score works</summary><div class=small><p>Points out of 100:</p><ul>"
             "<li><b>Momentum (30)</b>: last hour's volume vs its own 24h pace, price change in the last hour, still rising in the last 5 minutes.</li>"
             "<li><b>Buyers (12)</b>: share of trades that are buys, and unique buyers vs sellers (harder for bots to fake).</li>"
             "<li><b>Chatter (25)</b>: mentions on /biz/, Reddit and news (news counts most), CoinGecko and GeckoTerminal trending lists, "
             "community takeovers, and whether mentions jumped vs earlier scans. Paid DexScreener boosts add a little: they're ads, but ads move memes.</li>"
             "<li><b>Narrative (15)</b>: matches an upcoming catalyst (more points as the date gets closer), sits in a hot DexScreener narrative, "
             "or is named after something trending on Google.</li>"
             "<li><b>Room to run (12)</b> and <b>liquidity (6)</b>: $100k-$1M market caps score highest because a 10-100x needs a small start; "
             "deeper liquidity means you can actually sell.</li></ul>"
             "<p>Then penalties: thin liquidity, launched in the last hour, already up 300%+ in a day, no sells (honeypot), dumping right now, "
             "and safety failures from RugCheck (Solana) or GoPlus (Ethereum, Base, BNB). 'Danger' means things like the dev can still print tokens, "
             "freeze your wallet, or block sells.</p>"
             "<p><b>What it can't see:</b> X/Twitter and Telegram, where a lot of meme coin hype starts. X has no free way for a bot to read it, so "
             "CoinGecko trending, DexScreener boosts and on-chain buying stand in for it. Base and Solana coins may be buyable in the Coinbase app's "
             "built-in DEX trading.</p></div></details>")
    h.append("<details><summary>Data sources this scan</summary><div class=wrap><table class=small><tr><th>Source</th><th class=n>OK</th><th class=n>Failed</th><th>Note</th></tr>")
    for k in sorted(HEALTH):
        v = HEALTH[k]
        h.append(f"<tr><td>{esc(k)}</td><td class=n>{v['ok']}</td><td class=n>{v['fail']}</td><td class=muted>{esc(v['note'] if v['fail'] else '')}</td></tr>")
    h.append("</table></div></details><p class='muted small'>Not financial advice. Data from DexScreener, GeckoTerminal, CoinGecko, Coinbase, RugCheck, GoPlus, "
             "4chan, Reddit, CoinDesk, Cointelegraph, Decrypt, The Block, Google News and Google Trends.</p>")
    with open(os.path.join(OUT, "index.html.tmp"), "w") as f:
        f.write("\n".join(h))
    os.replace(os.path.join(OUT, "index.html.tmp"), os.path.join(OUT, "index.html"))


# ------------------------------------------------------------------ main
def main():
    today = datetime.now(ET_TZ).date()
    hist = load("history.json", {"scans": []})
    sec_cache = {k: v for k, v in load("security_cache.json", {}).items() if NOW - v.get("t", 0) < 86400}
    cb_seen = load("coinbase_listings.json", {})
    ledger = load("ledger.json", {"tickets": []})
    claude_picks = load("claude_picks.json", [])

    # phase 1: everything that doesn't depend on anything else, in parallel
    with ThreadPoolExecutor(max_workers=10) as ex:
        jobs = {"lists": ex.submit(f_ds_lists), "metas": ex.submit(f_ds_metas),
                "themes": ex.submit(f_ds_search, list(THEME_SEARCH.items())), "gt": ex.submit(f_gt), "cg": ex.submit(f_cg),
                "cb": ex.submit(f_cb), "biz": ex.submit(f_biz), "reddit": ex.submit(f_reddit), "news": ex.submit(f_news),
                "trends": ex.submit(f_trends)}
        raw = {}
        for k, fu in jobs.items():
            try:
                raw[k] = fu.result()
            except Exception as e:
                _mark("internal: " + k, False, f"{type(e).__name__}: {e}")
                raw[k] = None

    # ingest DEX lists
    for src, items in (raw["lists"] or {}).items():
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or not it.get("chainId") or not it.get("tokenAddress"):
                continue
            c = cand(it["chainId"], it["tokenAddress"])
            tag(c, src)
            if it.get("description") and not c.get("desc"):
                c["desc"] = str(it["description"])[:500]
            add_links(c, it.get("links"))
    metas = raw["metas"] or []
    for m in metas:
        for p in sorted(m["pairs"], key=lambda p: -num((p.get("volume") or {}).get("h24")))[:15]:
            c = take_pair(p, "dex_meta")
            if c and m["name"] not in [x[0] for x in c["metas"]]:
                c["metas"].append((m["name"], max(m["chg"]["h6"], m["chg"]["h1"])))
        m.pop("pairs", None)
    metas.sort(key=lambda m: -m["chg"]["h6"])
    for q, must, pairs in raw["themes"] or []:
        for p in pairs[:30]:
            bt = p.get("baseToken") or {}
            nm = ((bt.get("name") or "") + " " + (bt.get("symbol") or "")).lower()
            if any(w in nm for w in must) and num((p.get("liquidity") or {}).get("usd")) >= 3000:
                take_pair(p, "theme_search")
    for src, j in raw["gt"] or []:
        if not isinstance(j, dict):
            continue
        toks = {t.get("id"): t.get("attributes") or {} for t in j.get("included") or [] if t.get("type") == "token"}
        for d in j.get("data") or []:
            a, rel = d.get("attributes") or {}, d.get("relationships") or {}
            bid = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
            net = ((rel.get("network") or {}).get("data") or {}).get("id") or ""
            if "_" not in bid:
                continue
            addr, chain = bid.rsplit("_", 1)[1], GT2DS.get(net, net or bid.rsplit("_", 1)[0])
            ta = toks.get(bid, {})
            sym = (ta.get("symbol") or (a.get("name") or "").split(" / ")[0]).strip()
            if not sym or sym.upper() in MAJORS or is_stock(ta.get("name"), sym):
                continue
            c = cand(chain, addr)
            tag(c, src)
            tx = a.get("transactions") or {}
            c["gt"] = {"buyers_h1": num((tx.get("h1") or {}).get("buyers")), "sellers_h1": num((tx.get("h1") or {}).get("sellers"))}
            c["gt_attr"] = {"sym": sym, "name": ta.get("name") or sym, "a": a, "net": net}

    # CoinGecko + Coinbase
    trj, mmj = raw["cg"] or (None, None)
    cg_trend = []
    for i, it in enumerate((trj or {}).get("coins") or [] if isinstance(trj, dict) else []):
        x = it.get("item") or {}
        cg_trend.append({"id": x.get("id"), "symbol": (x.get("symbol") or "").upper(), "name": x.get("name") or "", "rank": i + 1})
    cg_categories = []
    for x in (trj or {}).get("categories") or [] if isinstance(trj, dict) else []:
        cg_categories.append({"name": x.get("name"), "chg24": num(((x.get("data") or {}).get("market_cap_change_percentage_24h") or {}).get("usd"))})
    cg_memes = [m for m in (mmj if isinstance(mmj, list) else []) if isinstance(m, dict)]
    cbprods = {}
    for p in ((raw["cb"] or {}).get("products") or []) if isinstance(raw["cb"], dict) else []:
        if (p.get("quote_currency_id") or p.get("quote_display_symbol")) != "USD":
            continue
        b = (p.get("base_display_symbol") or p.get("base_currency_id") or "").upper()
        if not b or p.get("trading_disabled") or p.get("is_disabled") or p.get("view_only"):
            continue
        cbprods[b] = {"pid": p.get("product_id"), "name": p.get("base_name") or b, "price": num(p.get("price")),
                      "chg24": num(p.get("price_percentage_change_24h")), "vol_usd": num(p.get("approximate_quote_24h_volume")),
                      "volchg24": num(p.get("volume_percentage_change_24h")), "new": bool(p.get("new"))}
    if cbprods:   # remember when each Coinbase listing first appeared
        first_run = not cb_seen
        for b in cbprods:
            if b not in cb_seen:
                cb_seen[b] = 0 if first_run else NOW
    fresh = sorted(((b, t) for b, t in cb_seen.items() if t and NOW - t < 21 * 86400 and b in cbprods), key=lambda x: -x[1])
    cb_new = [(b, datetime.fromtimestamp(t, ET_TZ).strftime("%b %d")) for b, t in fresh]
    cb_new = [(b, "marked new") for b, p in cbprods.items() if p["new"] and b not in dict(cb_new)] + cb_new

    # chatter docs + index
    docs = build_docs(raw["biz"] or [], raw["reddit"] or [], raw["news"] or [])
    idx = {"cash": {}, "upper": {}, "addr": {}}
    for i, d in enumerate(docs):
        for s in d.cash:
            idx["cash"].setdefault(s, []).append(i)
        for s in d.upper:
            idx["upper"].setdefault(s, []).append(i)
        for a in d.addrs:
            idx["addr"].setdefault(a, []).append(i)
    cash_count = sorted(((s, len(v)) for s, v in idx["cash"].items() if s.upper() not in MAJORS and s.upper() not in STOP), key=lambda x: -x[1])
    trends = raw["trends"] or []
    caps = {}
    for d in docs:
        if d.src == "biz":
            for w in d.upper:
                if w not in STOP and not w.isdigit():
                    caps[w] = caps.get(w, 0) + 1
    caps_top = sorted(caps.items(), key=lambda x: -x[1])[:60]

    # phase 2: searches that depend on phase 1 (chatter tickers, CoinGecko trending, Google Trends terms), Coinbase hourly bars
    known_syms = {c["symbol"].lower() for c in C.values() if c["symbol"]}
    qs = []
    for s, n in cash_count[:25]:
        if n >= 3 and s not in known_syms and len(qs) < 10:
            qs.append((s, "chatter_search", s.upper()))
    for t in cg_trend:
        if t["symbol"].lower() not in known_syms and t["symbol"] not in MAJORS:
            qs.append((t["name"] or t["symbol"], "cg_search", t["symbol"]))
    trends.sort(key=lambda t: -traffic_n(t["traffic"]))
    for t in trends[:8]:
        qs.append((t["term"], "trend_search", t["term"]))
    meme_syms = {m.get("symbol", "").upper(): m for m in cg_memes}
    if not meme_syms:
        meme_syms = {s: {} for s in "DOGE SHIB PEPE BONK WIF FLOKI POPCAT MOG PENGU TRUMP TURBO BRETT TOSHI MEW PNUT FARTCOIN SPX MOODENG GIGA DEGEN".split()}
    cb_meme_pids = [cbprods[s]["pid"] for s in meme_syms if s in cbprods]

    def run_searches():
        res = []
        for q, src, want in qs:
            j = fetch(DS + "/latest/dex/search?q=" + urllib.parse.quote(q), "DexScreener search")
            res.append((q, src, want, (j or {}).get("pairs") or [] if isinstance(j, dict) else []))
        return res

    with ThreadPoolExecutor(max_workers=2) as ex:
        fs, fh = ex.submit(run_searches), ex.submit(f_cb_hourly, cb_meme_pids[:45])
        sres, cb_hour = fs.result(), fh.result()
    trend_coins = {}
    for q, src, want, pairs in sres:
        if src == "trend_search":
            # a coin "named after" a trend: its name holds the trend's most distinctive words, and it launched this week
            words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) >= 4 and w.upper() not in STOP and w not in COMMON_WORDS]
            words = sorted(words, key=len, reverse=True)[:2]
            got = []
            for p in pairs[:30]:
                bt = p.get("baseToken") or {}
                nm = re.sub(r"[^a-z0-9]", "", ((bt.get("name") or "") + (bt.get("symbol") or "")).lower())
                fresh = NOW - num(p.get("pairCreatedAt")) / 1000 < 7 * 86400
                if words and all(w in nm for w in words) and fresh and num((p.get("liquidity") or {}).get("usd")) >= 3000:
                    c = take_pair(p, "trend_search")
                    if c:
                        c["culture"] = q
                        got.append(c["key"])
            trend_coins[q] = list(dict.fromkeys(got))
        else:
            best = None
            for p in pairs[:30]:
                bt = p.get("baseToken") or {}
                if (bt.get("symbol") or "").upper() == want and (best is None or num((p.get("liquidity") or {}).get("usd")) > num((best.get("liquidity") or {}).get("usd"))):
                    best = p
            if best and num((best.get("liquidity") or {}).get("usd")) >= 10000:
                take_pair(best, src)

    # fill every DEX candidate with full pool data (all pools, summed volume)
    by_chain = {}
    for c in C.values():
        if c["chain"] and c["address"]:
            by_chain.setdefault(c["chain"], []).append(c["address"])
    for ch, addrs in by_chain.items():
        for i in range(0, len(addrs), 30):
            j = fetch(f"{DS}/tokens/v1/{ch}/" + ",".join(addrs[i:i + 30]), "DexScreener tokens")
            for p in j if isinstance(j, list) else []:
                take_pair(p)
    for c in C.values():   # chains DexScreener doesn't cover: fall back to GeckoTerminal's numbers
        g = c.get("gt_attr")
        if c["has_data"] or not g:
            continue
        a = g["a"]
        tx = a.get("transactions") or {}
        pc, vu = a.get("price_change_percentage") or {}, a.get("volume_usd") or {}
        c.update(symbol=g["sym"], name=g["name"], price=num(a.get("base_token_price_usd")), mcap=num(a.get("market_cap_usd")) or num(a.get("fdv_usd")),
                 liq=num(a.get("reserve_in_usd")), chg={k: num(pc.get(k)) for k in ("m5", "h1", "h6", "h24")},
                 vol={k: num(vu.get(k)) for k in ("m5", "h1", "h6", "h24")},
                 txns={k: [int(num((tx.get(k) or {}).get("buys"))), int(num((tx.get(k) or {}).get("sells")))] for k in ("m5", "h1", "h6", "h24")},
                 created=parse_time(a.get("pool_created_at")), pair_url=f"https://www.geckoterminal.com/{g['net']}/pools/{a.get('address')}", has_data=True)

    # phase 3: chatter, themes, Coinbase/CoinGecko links, first-pass score
    cg_rank = {(t["symbol"], t["name"].lower()): t["rank"] for t in cg_trend}
    cg_rank_sym = {t["symbol"]: t["rank"] for t in cg_trend}
    prev_ment = {}
    recent_scans = [s for s in hist.get("scans", []) if NOW - s["t"] < 6 * 3600]
    for s in recent_scans:
        for k, v in s["c"].items():
            prev_ment.setdefault(k, []).append(v[1])
    prev_ment = {k: sum(v) / len(recent_scans) for k, v in prev_ment.items()} if recent_scans else {}
    ctx = {"today": today, "prev_ment": prev_ment}
    live = []
    for c in C.values():
        if not c["has_data"] or c["vol"]["h24"] < 5000 or c["price"] <= 0:
            continue
        c["ment"] = mentions(c, docs, idx)
        text = " ".join([c["name"], c["symbol"], c.get("desc") or "", " ".join(c["links"])])
        c["themes"] = theme_hits(text)
        for th, v in THEMES.items():
            if c["chain"] in v.get("chains", []):
                c["themes"].add(th)
        r = cg_rank.get((c["symbol"].upper(), c["name"].lower())) or (cg_rank_sym.get(c["symbol"].upper()) if c["mcap"] > 5e6 else None)
        if r:
            c["cg_rank"] = r
        pr = cbprods.get(c["symbol"].upper())
        if pr and pr["name"].lower()[:4] == c["name"].lower()[:4]:
            c["on_cb"] = True
        score(c, ctx)
        live.append(c)
    live.sort(key=lambda c: -c["score"])
    # phase 4: safety checks on the top of the list, then final score
    to_check = [c for c in live if c["mcap"] < 2e9][:40]
    with ThreadPoolExecutor(max_workers=3) as ex:
        sol = [c for c in to_check if c["chain"] == "solana"]
        evm = [c for c in to_check if c["chain"] in EVM_ID]
        for grp in (sol, evm):
            for c, res in zip(grp, ex.map(lambda c: security(c, sec_cache), grp)):
                c["sec"] = res
    groups = {}
    for c in live:
        for g in {re.sub(r"[^a-z0-9]", "", c["symbol"].lower()), re.sub(r"[^a-z0-9]", "", c["name"].lower())}:
            if len(g) >= 3:
                groups.setdefault(g, []).append(c)
    for c in live:
        twins = {x["key"] for g in {re.sub(r"[^a-z0-9]", "", c["symbol"].lower()), re.sub(r"[^a-z0-9]", "", c["name"].lower())}
                 for x in groups.get(g, []) if x["key"] != c["key"]}
        if twins:
            older = [C[k] for k in twins if C[k].get("created") and c.get("created") and C[k]["created"] < c["created"]]
            c["copycats"] = len(twins)
            c["copy_younger"] = bool(older)
            c["copy_note"] = (f"{len(twins)} other coin{'s' if len(twins) > 1 else ''} use this name; "
                              + ("this is NOT the oldest: check the contract" if older else "this is the oldest one"))
    for c in live:
        if "sec" not in c and c["key"] in sec_cache:
            c["sec"] = sec_cache[c["key"]]
        score(c, ctx)
    live.sort(key=lambda c: (-c["score"], -c["vol"]["h1"]))
    old = [s for s in hist.get("scans", []) if 5 * 3600 <= NOW - s["t"] <= 7 * 3600]
    if old:
        ref = min(old, key=lambda s: abs(NOW - s["t"] - 6 * 3600))
        for c in live:
            if c["key"] in ref["c"]:
                c["score_6h"] = ref["c"][c["key"]][0]
    dangerous = lambda c: (c.get("sec") or {}).get("verdict") == "danger" or flag_note(c)
    ok_live = [c for c in live if not dangerous(c) and c["mcap"] < 1e9]
    seen_names, uniq = set(), []
    for c in ok_live:        # one row per name: copycats collapse into the best-scoring one
        nm = re.sub(r"[^a-z0-9]", "", c["name"].lower()) or c["key"]
        if nm not in seen_names:
            seen_names.add(nm)
            uniq.append(c)
    top = [c for c in uniq if c["score"] >= 45][:10] or uniq[:5]
    flagged = sorted([c for c in live if dangerous(c) and (c["ment"]["n"] or c["vol"]["h1"] > 20000 or flag_note(c))], key=lambda c: -c["vol"]["h24"])

    # Coinbase memes
    cbm = []
    for s, m in meme_syms.items():
        pr = cbprods.get(s)
        if not pr:
            continue
        e = {"symbol": s, "name": m.get("name") or pr["name"], "pid": pr["pid"], "price": pr["price"], "chg24": pr["chg24"],
             "chg1h": num(m.get("price_change_percentage_1h_in_currency")) or (cb_hour.get(pr["pid"]) or {}).get("chg1h", 0.0),
             "chg7d": num(m.get("price_change_percentage_7d_in_currency")), "mcap": num(m.get("market_cap")), "vol_usd": pr["vol_usd"],
             "volchg24": pr["volchg24"], "spike": (cb_hour.get(pr["pid"]) or {}).get("spike", 0.0), "new": pr["new"] or bool(cb_seen.get(s) and NOW - cb_seen[s] < 14 * 86400)}
        pseudo = {"symbol": s, "name": e["name"], "address": "", "chain": "coinbase"}
        e["ment"] = mentions(pseudo, docs, idx)
        e["themes"] = theme_hits(e["name"] + " " + s)
        e["cg_rank"] = cg_rank_sym.get(s)
        cb_heat(e, ctx)
        cbm.append(e)
    cbm.sort(key=lambda e: -e["heat"])

    # catalysts table
    cats = []
    news_docs = [d for d in docs if d.src == "news"]
    trend_text = " ".join(t["term"] + " " + " ".join(t["news"]) for t in trends).lower()
    for cat in CATALYSTS:
        d = (date.fromisoformat(cat["date"]) - today).days
        if d < -1 or d > 75:
            continue
        rx = THEME_RE[cat["theme"]]
        heads = sum(1 for x in news_docs if rx.search(x.low)) + len(rx.findall(trend_text))
        coins = sorted([c for c in live if cat["theme"] in c["themes"]], key=lambda c: -c["score"])
        cats.append({"date": cat["date"], "event": cat["event"], "days": d, "heads": heads, "coins": coins})
    for t in trends:
        t["coins"] = [C[k] for k in trend_coins.get(t["term"], []) if k in C and C[k].get("score") is not None]
        t["coins"].sort(key=lambda c: -c["vol"]["h24"])

    # chatter leaderboard
    sym2c = {}
    for c in live:
        s = c["symbol"].lower()
        if s not in sym2c or c["vol"]["h24"] > sym2c[s]["vol"]["h24"]:
            sym2c[s] = c
    prev_tick = {}
    for s in recent_scans:
        for k, v in (s.get("tick") or {}).items():
            prev_tick.setdefault(k, []).append(v)
    rows = {}
    for c in sorted(live, key=lambda c: -c["ment"]["n"]):
        s = c["symbol"].upper()
        if c["ment"]["n"] >= 2 and s not in rows:
            rows[s] = {"sym": s, "biz": c["ment"]["biz"], "reddit": c["ment"]["reddit"], "news": c["ment"]["news"], "n": c["ment"]["n"], "c": c}
    for s, n in cash_count[:40]:
        if n >= 2 and s.upper() not in rows:
            ids = set(idx["cash"].get(s, ()))
            rows[s.upper()] = {"sym": s.upper(), "biz": sum(docs[i].src == "biz" for i in ids), "reddit": sum(docs[i].src == "reddit" for i in ids),
                               "news": sum(docs[i].src == "news" for i in ids), "n": len(ids), "c": sym2c.get(s)}
    chat = sorted(rows.values(), key=lambda r: -r["n"])[:40]
    for r in chat:
        pv = prev_tick.get(r["sym"].lower())
        r["before"] = sum(pv) / len(recent_scans) if pv else (0 if recent_scans else None)

    # ledger, history
    ledger = update_ledger(ledger, live, cbm, cbprods, claude_picks)
    snap = {"t": NOW, "c": {c["key"]: [c["score"], c["ment"]["n"], c["price"]] for c in live[:120] if c["score"] >= 30 or c["ment"]["n"]},
            "tick": {r["sym"].lower(): r["n"] for r in chat[:40]}}
    hist["scans"] = [s for s in hist.get("scans", []) if NOW - s["t"] < 26 * 3600] + [snap]

    n_ok = sum(1 for v in HEALTH.values() if v["ok"])
    page_ctx = {"top": top, "cbm": cbm, "metas": metas, "trends": trends, "chat": chat, "ledger": ledger, "flagged": flagged,
                "catalysts": cats, "cg_categories": cg_categories, "cb_new": cb_new, "n_scanned": len(live), "n_sources": len(HEALTH),
                "n_sources_ok": n_ok, "secs": time.time() - START}
    write_page(page_ctx)

    def slim(c):
        return {k: c.get(k) for k in ("key", "symbol", "name", "chain", "address", "pair_url", "price", "mcap", "liq", "vol", "chg", "txns",
                                      "created", "score", "pts", "pen", "why", "warn", "ment", "themes", "metas", "culture", "cg_rank",
                                      "on_cb", "sec", "src", "links", "desc", "score_6h", "copy_note")}
    save("scan.json", {"t": NOW, "updated": datetime.fromtimestamp(NOW, ET_TZ).isoformat(), "health": HEALTH, "top": [slim(c) for c in live[:60]],
                       "flagged": [slim(c) for c in flagged[:20]], "coinbase_memes": cbm[:30], "metas": metas[:15],
                       "trends": [{k: v for k, v in t.items() if k != "coins"} | {"coins": [c["key"] for c in t.get("coins", [])[:6]]} for t in trends[:15]],
                       "chatter": [{k: v for k, v in r.items() if k != "c"} | {"coin": (r["c"] or {}).get("key")} for r in chat[:30]],
                       "catalysts": [{k: v for k, v in x.items() if k != "coins"} | {"coins": [c["key"] for c in x["coins"][:8]]} for x in cats],
                       "cg_trending": cg_trend, "cg_categories": cg_categories, "cb_new": cb_new[:20],
                       "n_docs": {s: sum(d.src == s for d in docs) for s in SRC_W}, "secs": round(time.time() - START, 1),
                       "debug": {"cashtags": cash_count[:40], "caps_biz": caps_top, "biz_threads": BIZ_THREADS}}, pretty=True)
    save("history.json", hist)
    save("security_cache.json", sec_cache)
    if cbprods:
        save("coinbase_listings.json", cb_seen)
    save("ledger.json", ledger, pretty=True)
    print(f"meme scan: {len(live)} coins, top={[(c['symbol'], c['score']) for c in top[:5]]}, sources ok {n_ok}/{len(HEALTH)}, {time.time() - START:.0f}s")
    for k, v in sorted(HEALTH.items()):
        print(f"  {k}: ok {v['ok']} fail {v['fail']} {v['note'] if v['fail'] else ''}")


if __name__ == "__main__":
    main()
