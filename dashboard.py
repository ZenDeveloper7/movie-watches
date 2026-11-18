"""Movie watch dashboard: add, edit, pause, mute and delete changedetection watches
that poll the bms-seats helper, without hand-writing helper URLs.

Talks to changedetection's REST API (CD_URL + CD_API_KEY). Every watch it creates or
edits gets its own check interval (time_between_check_use_default=false; the API
default silently falls back to the global 3-hour interval) and "added lines only".
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import apprise
import requests

CD_URL = os.environ.get("CD_URL", "http://changedetection:5000").rstrip("/")
CD_KEY = os.environ["CD_API_KEY"]
HELPER = os.environ.get("HELPER_URL", "http://bms-seats:8000").rstrip("/")
CITY = os.environ.get("CITY", "hyderabad")
HEALTH_EVERY = int(os.environ.get("HEALTH_CHECK_SECONDS", "300"))
HEALTH_STATE = os.environ.get("HEALTH_STATE_FILE", "/data/dashboard-health.json")
HERE = os.path.dirname(os.path.abspath(__file__))
BMS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/87.0.4280.66 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Watches whose URL was edited: muted until their first check with the new URL,
# so the changed helper text does not send a spurious alert.
pending_unmute = {}
pending_lock = threading.Lock()
cache = {}


def cd(method, path, body=None, params=None):
    r = requests.request(method, f"{CD_URL}/api/v1{path}", headers={"x-api-key": CD_KEY},
                         json=body, params=params, timeout=30)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return r.text


def cached(key, ttl, fn):
    hit = cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    cache[key] = (time.time(), val)
    return val


def list_watches():
    out = []
    for uid, w in cd("GET", "/watch").items():
        full = cd("GET", f"/watch/{uid}")
        tbc = full.get("time_between_check") or {}
        minutes = (int(tbc.get("weeks") or 0) * 10080 + int(tbc.get("days") or 0) * 1440
                   + int(tbc.get("hours") or 0) * 60 + int(tbc.get("minutes") or 0))
        out.append({
            "id": uid, "title": w.get("title") or w.get("url"), "url": w.get("url"),
            "paused": bool(w.get("paused")), "muted": bool(w.get("notification_muted")),
            "last_checked": w.get("last_checked") or 0, "last_changed": w.get("last_changed") or 0,
            "error": w.get("last_error") or "", "interval": minutes,
            "uses_default": bool(full.get("time_between_check_use_default")),
            "pending_unmute": uid in pending_unmute,
        })
    out.sort(key=lambda w: w["title"].lower())
    return out


def watch_body(d):
    url = d["url"].strip()
    if not re.match(r"https?://", url):
        raise ValueError("URL must start with http:// or https://")
    minutes = max(1, int(d.get("interval") or 5))
    return {"url": url, "title": (d.get("title") or url).strip(),
            "time_between_check_use_default": False, "time_between_check": {"minutes": minutes}}


def create_watch(d):
    body = watch_body(d) | {"fetch_backend": "system", "processor": "text_json_diff",
                            "filter_text_added": True, "filter_text_removed": False,
                            "filter_text_replaced": True}
    return cd("POST", "/watch", body)


def update_watch(uid, d):
    body = watch_body(d)
    old = cd("GET", f"/watch/{uid}")
    url_changed = old.get("url") != body["url"]
    if url_changed and not old.get("notification_muted"):
        cd("GET", f"/watch/{uid}", params={"muted": "muted"})
        with pending_lock:
            pending_unmute[uid] = time.time()
    cd("PUT", f"/watch/{uid}", body)
    if url_changed:
        cd("GET", f"/watch/{uid}", params={"recheck": "1"})
    return {"ok": True, "muted_until_recheck": url_changed}


def unmute_loop():
    while True:
        time.sleep(15)
        with pending_lock:
            items = list(pending_unmute.items())
        for uid, since in items:
            try:
                w = cd("GET", f"/watch/{uid}")
                if (w.get("last_checked") or 0) > since + 1:
                    cd("GET", f"/watch/{uid}", params={"muted": "unmuted"})
                    with pending_lock:
                        pending_unmute.pop(uid, None)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    with pending_lock:
                        pending_unmute.pop(uid, None)
            except Exception as e:
                print(f"unmute check failed for {uid}: {e!r}", flush=True)


def bms_movies():
    """Movies on BMS now-showing + upcoming pages for the city: [{title, slug, event}]."""
    seen = {}
    for page in (f"explore/movies-{CITY}", f"explore/upcoming-movies-{CITY}"):
        t = requests.get(f"https://in.bookmyshow.com/{page}", headers=BMS_HEADERS, timeout=30).text
        for slug, ev, name in re.findall(
                r'"url":"https://in\.bookmyshow\.com/(?:[a-z-]+/)?movies/(?:[a-z-]+/)?([a-z0-9-]+)/(ET\d+)","name":"([^"]+)"', t):
            seen.setdefault(ev, {"title": name, "slug": slug, "event": ev})
        for slug, ev in re.findall(r'/movies/(?:[a-z-]+/)?([a-z0-9-]+)/(ET\d{8})', t):
            seen.setdefault(ev, {"title": slug.replace("-", " ").title(), "slug": slug, "event": ev})
    return sorted(seen.values(), key=lambda m: m["title"].lower())


def bms_movie(event):
    t = requests.get(f"https://in.bookmyshow.com/movies/{CITY}/x/{event}",
                     headers=BMS_HEADERS, timeout=30).text
    title = (re.findall(r'"meta":\{"title":"([^"]+)","subtitle":"Select language and format"', t)
             or re.findall(r'"name":"([^"]+)","url":"[^"]*' + event, t) or [event])[0]
    rel = (re.findall(r'"releaseDate":"([^"]+)"', t) or [""])[0]
    listed = re.findall(r'"language":"([^"]+)","genre":"[^"]+","format":"([^"]+)"', t)
    opts = set()
    for lang, fmts in re.findall(r'\{"language":"([^"]+)","formats":\[(.*?)\]\}', t):
        for dim, code in re.findall(r'"dimension":"([^"]+)","eventCode":"(ET\d+)"', fmts):
            opts.add((lang, dim, code))
    return {"event": event, "title": title, "release": rel,
            "listed_languages": listed[0][0] if listed else "",
            "listed_formats": listed[0][1] if listed else "",
            "bookable": [{"language": l, "format": f, "event": c} for l, f, c in sorted(opts)],
            "book_button": '"bookButton":true' in t,
            "poster": (lambda m: f"/api/img?u={requests.utils.quote(m.group(0), safe='')}" if m else "")(
                re.search(r"https://assets-in\.bmscdn\.com/iedb/movies/images/mobile/thumbnail/xlarge/[a-z0-9-]*"
                          + event.lower() + r"-\d+\.jpg", t))}


IMG_HOSTS = ("assets-in.bmscdn.com", "cdn.district.in")
img_cache = {}


def words(s):
    return re.findall(r"[a-z0-9]+", s.lower().replace("'", ""))


def best_match(name, entries):
    """Entry whose title contains every word of `name`, preferring the fewest extra words."""
    want = words(name)
    if not want:
        return None
    hits = [(len(words(e["title"])) - len(want), i, e) for i, e in enumerate(entries)
            if all(w in words(e["title"]) for w in want)]
    return min(hits)[2] if hits else None


def bms_poster(event):
    t = requests.get(f"https://in.bookmyshow.com/movies/{CITY}/x/{event}", headers=BMS_HEADERS, timeout=30).text
    m = re.search(r"https://assets-in\.bmscdn\.com/iedb/movies/images/mobile/thumbnail/xlarge/[a-z0-9-]*"
                  + event.lower() + r"-\d+\.jpg", t)
    return m.group(0) if m else ""


def district_catalog():
    t = requests.get(f"https://www.district.in/movies/upcoming-movies-in-{CITY}", headers=BMS_HEADERS, timeout=30).text
    return [{"title": n, "image": i} for n, i in
            re.findall(r'"@type":"Movie","url":"[^"]+","name":"([^"]+)","image":"([^"]+)"', t)]


def resolve_poster(g):
    """g: {name, events: [ET...], keywords: [...]}. Returns an image URL or ""."""
    for ev in g.get("events") or []:
        url = cached(("poster", ev), 21600, lambda: bms_poster(ev))
        if url:
            return url
    names = [g.get("name") or ""] + [k.replace("-", " ") for k in g.get("keywords") or []]
    try:
        bms = cached("movies", 1800, bms_movies)
    except Exception:
        bms = []
    for n in names:
        m = best_match(n, bms)
        if m:
            url = cached(("poster", m["event"]), 21600, lambda: bms_poster(m["event"]))
            if url:
                return url
    try:
        dist = cached("district_catalog", 21600, district_catalog)
    except Exception:
        dist = []
    for n in names:
        m = best_match(n, dist)
        if m:
            return m["image"]
    return ""


def posters(groups):
    out = {}
    for g in groups[:40]:
        try:
            url = cached(("group-poster", json.dumps(g, sort_keys=True)), 21600, lambda: resolve_poster(g))
        except Exception as e:
            print(f"poster lookup failed for {g.get('name')}: {e!r}", flush=True)
            url = ""
        out[g.get("name", "")] = f"/api/img?u={requests.utils.quote(url, safe='')}" if url else ""
    return out


def image(url):
    host = urlparse(url).hostname or ""
    if not url.startswith("https://") or host not in IMG_HOSTS:
        raise ValueError("Image host not allowed")
    hit = img_cache.get(url)
    if hit:
        return hit
    r = requests.get(url, headers=BMS_HEADERS, timeout=30)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "image/jpeg")
    if not ctype.startswith("image/"):
        raise ValueError("Not an image")
    if len(img_cache) > 80:
        img_cache.clear()
    img_cache[url] = (r.content, ctype)
    return img_cache[url]


def parse_release(text):
    for fmt in ("%b %d, %Y", "%d %b, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


def releases(days_back=3, days_ahead=7):
    """BookMyShow movies in the city releasing from `days_back` days ago to `days_ahead`
    days from now, with poster, languages, formats and whether booking is open."""
    today = date.today()
    lo, hi = today - timedelta(days=days_back), today + timedelta(days=days_ahead)
    listing = cached("movies", 1800, bms_movies)

    def detail(m):
        try:
            return m, cached(("movie", m["event"]), 1800, lambda: bms_movie(m["event"]))
        except Exception as e:
            print(f"release detail {m['event']} failed: {e!r}", flush=True)
            return m, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        found = list(pool.map(detail, listing))
    out = []
    for m, d in found:
        rel = parse_release(d["release"]) if d and d.get("release") else None
        if not rel or not lo <= rel <= hi:
            continue
        out.append({"event": m["event"], "slug": m["slug"], "title": d["title"] if d["title"] != m["event"] else m["title"],
                    "release": rel.isoformat(), "languages": d["listed_languages"], "formats": d["listed_formats"],
                    "bookable": d["bookable"], "open": d["book_button"], "poster": d.get("poster", "")})
    # One entry per movie: language versions have their own event codes.
    merged = {}
    for r in out:
        k = r["title"].lower()
        if k not in merged:
            merged[k] = r
            continue
        m = merged[k]
        m["open"] = m["open"] or r["open"]
        seen = {b["event"] for b in m["bookable"]}
        m["bookable"] += [b for b in r["bookable"] if b["event"] not in seen]
    out = list(merged.values())
    out.sort(key=lambda r: (r["release"], not r["open"], r["title"].lower()))
    return out


def current_shows(url):
    """Helper text for a watch: the reply changedetection last got (no extra BookMyShow
    traffic), or a fresh fetch when that is missing or older than 15 minutes."""
    if not url.startswith(HELPER + "/"):
        raise ValueError("Only movie watches list shows")
    path = url[len(HELPER):]
    try:
        r = requests.get(f"{HELPER}/last", params={"path": path}, timeout=10)
        if r.ok:
            hit = r.json()
            if hit["age"] < 900:
                return {"text": hit["text"], "age": hit["age"]}
    except (requests.RequestException, ValueError):
        pass
    text = cached(("live", url), 180, lambda: preview(url))
    return {"text": text, "age": 0}


# ---------- health monitor: Telegram message when a watch stops working ----------

def load_health():
    try:
        with open(HEALTH_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_health(st):
    try:
        os.makedirs(os.path.dirname(HEALTH_STATE), exist_ok=True)
        with open(HEALTH_STATE + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(HEALTH_STATE + ".tmp", HEALTH_STATE)
    except OSError as e:
        print(f"saving health state failed: {e!r}", flush=True)


def problem(w, now):
    if w["paused"]:
        return ""
    if w["uses_default"]:
        return "uses the global 3-hour check interval instead of its own"
    if w["error"]:
        return f"last check failed: {str(w['error'])[:160]}"
    if w["last_checked"] and now - w["last_checked"] > max(w["interval"] * 180, 900):
        mins = round((now - w["last_checked"]) / 60)
        return f"hasn't been checked for {mins} min (should be every {w['interval']} min)"
    return ""


def notify(lines):
    urls = cd("GET", "/notifications").get("notification_urls") or []
    if not urls:
        print("health: no notification URL configured in changedetection", flush=True)
        return False
    ap = apprise.Apprise()
    for u in urls:
        ap.add(u)
    return ap.notify(title="Movie Watches", body="\n".join(lines))


def health_loop():
    """A problem has to show up on two checks in a row before it is reported, then it
    is reported once; recovery is reported once too."""
    st = load_health()  # watch id -> {"seen": n, "sent": bool, "title": str}
    while True:
        time.sleep(HEALTH_EVERY)
        try:
            ws, now = list_watches(), time.time()
        except Exception as e:
            print(f"health: changedetection unreachable: {e!r}", flush=True)
            continue
        bad, good = [], []
        alive = {w["id"] for w in ws}
        for w in ws:
            why, s = problem(w, now), st.get(w["id"])
            if why:
                s = st.setdefault(w["id"], {"seen": 0, "sent": False})
                s["seen"] += 1
                s["title"] = w["title"]
                if s["seen"] >= 2 and not s["sent"]:
                    bad.append(f"⚠️ {w['title']}: {why}")
                    s["sent"] = True
            elif s:
                if s.get("sent"):
                    good.append(f"✅ {w['title']}: checking again")
                st.pop(w["id"])
        for gone in set(st) - alive:
            st.pop(gone)
        if bad or good:
            try:
                notify(bad + good + ["", "Fix it in the dashboard."])
            except Exception as e:
                print(f"health: notify failed: {e!r}", flush=True)
        save_health(st)


def preview(url):
    if not url.startswith(HELPER + "/"):
        raise ValueError("Preview only works for bms-seats helper URLs")
    r = requests.get(url, timeout=180)
    return r.text


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/config":
            return self.json({"helper": HELPER, "city": CITY, "changedetection": os.environ.get("CD_PUBLIC_URL", "")})
        if u.path == "/api/watches":
            return self.wrap(list_watches)
        if u.path == "/api/movies":
            return self.wrap(lambda: cached("movies", 1800, bms_movies))
        m = re.fullmatch(r"/api/movie/(ET\d+)", u.path)
        if m:
            return self.wrap(lambda: cached(("movie", m.group(1)), 600, lambda: bms_movie(m.group(1))))
        if u.path == "/api/img":
            try:
                body, ctype = image(q.get("u", [""])[0])
            except Exception as e:
                return self.json({"error": str(e)}, 404)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return self.wfile.write(body)
        if u.path == "/api/releases":
            return self.wrap(lambda: cached("releases", 900, releases))
        if u.path == "/api/shows":
            return self.wrap(lambda: current_shows(q.get("url", [""])[0]))
        if u.path == "/api/preview":
            return self.wrap(lambda: {"text": preview(q.get("url", [""])[0])})
        self.json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        d = self.body()
        if u.path == "/api/watches":
            return self.wrap(lambda: create_watch(d))
        if u.path == "/api/posters":
            return self.wrap(lambda: posters(d.get("groups") or []))
        m = re.fullmatch(r"/api/watches/([\w-]+)/(pause|unpause|mute|unmute|recheck)", u.path)
        if m:
            uid, act = m.groups()
            params = {"pause": {"paused": "paused"}, "unpause": {"paused": "unpaused"},
                      "mute": {"muted": "muted"}, "unmute": {"muted": "unmuted"},
                      "recheck": {"recheck": "1"}}[act]
            if act in ("mute", "unmute"):
                with pending_lock:
                    pending_unmute.pop(uid, None)
            return self.wrap(lambda: (cd("GET", f"/watch/{uid}", params=params), {"ok": True})[1])
        self.json({"error": "not found"}, 404)

    def do_PUT(self):
        m = re.fullmatch(r"/api/watches/([\w-]+)", urlparse(self.path).path)
        if not m:
            return self.json({"error": "not found"}, 404)
        d = self.body()
        self.wrap(lambda: update_watch(m.group(1), d))

    def do_DELETE(self):
        m = re.fullmatch(r"/api/watches/([\w-]+)", urlparse(self.path).path)
        if not m:
            return self.json({"error": "not found"}, 404)
        uid = m.group(1)
        with pending_lock:
            pending_unmute.pop(uid, None)
        self.wrap(lambda: (cd("DELETE", f"/watch/{uid}"), {"ok": True})[1])

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def wrap(self, fn):
        try:
            self.json(fn())
        except requests.HTTPError as e:
            detail = e.response.text[:300] if e.response is not None else str(e)
            self.json({"error": f"{e} {detail}"}, 502)
        except Exception as e:
            print(f"{self.path} failed: {e!r}", flush=True)
            self.json({"error": str(e)}, 400)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj).encode(), "application/json")

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if "/api/watches" not in (args[0] if args else ""):
            print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    threading.Thread(target=unmute_loop, daemon=True).start()
    threading.Thread(target=health_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
