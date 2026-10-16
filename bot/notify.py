"""
Phone alerts for the meme scanner, sent through ntfy (free push notifications: https://ntfy.sh).

The topic name is a GitHub secret (NTFY_TOPIC) so nobody reading this public repo can subscribe to it or,
worse, push fake "buy this" alerts to your phone. Without the secret, alerts are still logged to the
dashboard, just not pushed.

Every alert is logged with the time it fired, so the dashboard can show whether alerts were worth acting on.
"""
import json, os, time, urllib.request

TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
MAX_LOG = 150


def send(title, message, url=None, priority=4, tags=None):
    """Push one notification. Returns True if ntfy accepted it."""
    if not TOPIC:
        return False
    body = {"topic": TOPIC, "title": title[:120], "message": message[:900], "priority": priority, "tags": tags or []}
    if url and url.startswith("https://"):
        body["click"] = url
        body["actions"] = [{"action": "view", "label": "Open chart", "url": url, "clear": False}]
    for k in range(3):
        try:
            req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "meme-scanner/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 <= r.status < 300
        except Exception as e:
            print("ntfy send failed:", type(e).__name__, str(e)[:120])
            time.sleep(2 + 3 * k)
    return False


class AlertBook:
    """Dedupe + log. One book per job (scanner / watcher) so the two never write the same file."""

    def __init__(self, path, other_paths=()):
        self.path = path
        try:
            with open(path) as f:
                self.d = json.load(f)
        except Exception:
            self.d = {}
        self.d.setdefault("sent", {})      # dedupe key -> time sent
        self.d.setdefault("log", [])
        self.d.setdefault("seen_news", [])
        self.other = {}
        for p in other_paths:              # read-only view of the other job's sends, so we don't double-alert
            try:
                with open(p) as f:
                    self.other.update(json.load(f).get("sent", {}))
            except Exception:
                pass
        now = time.time()
        self.d["sent"] = {k: t for k, t in self.d["sent"].items() if now - t < 7 * 86400}

    def recently(self, key, hours):
        t = max(self.d["sent"].get(key, 0), self.other.get(key, 0))
        return time.time() - t < hours * 3600

    def count_today(self, kind):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        return sum(1 for a in self.d["log"] if a.get("kind") == kind and a.get("day") == day)

    def alert(self, key, kind, title, message, url=None, priority=4, tags=None, coin=None, price=None):
        pushed = send(title, message, url, priority, tags)
        now = time.time()
        self.d["sent"][key] = now
        self.d["log"].append({"t": now, "day": time.strftime("%Y-%m-%d", time.gmtime()), "kind": kind, "title": title,
                              "message": message, "url": url, "pushed": pushed, "coin": coin, "price": price})
        self.d["log"] = self.d["log"][-MAX_LOG:]
        print(("PUSHED " if pushed else "LOGGED ") + title)
        return pushed

    def save(self):
        self.d["topic_set"] = bool(TOPIC)
        with open(self.path + ".tmp", "w") as f:
            json.dump(self.d, f, indent=1)
        os.replace(self.path + ".tmp", self.path)
