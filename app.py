"""BookMyShow seat-release helper for changedetection.io.

changedetection polls /releases/<eventCode>/<YYYYMMDD>/<venueCode>. Each poll
fetches the seat map of every show of that movie at that theatre, compares it
with the previous poll, and appends a line when enough seats flipped from
unavailable to available. The page text only changes when such a line is added,
so every change changedetection sees is worth a notification.

/status/<eventCode>/<YYYYMMDD>/<venueCode> shows the counts from the last poll.
"""
import base64
import json
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = os.environ["BMS_SEAT_KEY"].encode()
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Kolkata"))
DEFAULT_MIN = int(os.environ.get("MIN_SEATS", "10"))
MIN_POLL_GAP = 60  # seconds; repeat calls inside this window reuse the last result
MAX_EVENTS = 100
AVAILABLE = {"1", "4"}  # 1 = available, 4 = bestseller (still bookable)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/87.0.4280.66 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

CITY_SLUGS = {"HYD": "hyderabad", "MUMBAI": "mumbai", "NCR": "national-capital-region-ncr",
              "BANG": "bengaluru", "CHEN": "chennai", "KOLK": "kolkata", "PUNE": "pune"}
MAX_PAIRS = 4  # side-by-side pairs listed per alert

lock = threading.Lock()
# Last successful reply per request path (what changedetection saw), so the dashboard
# can show current shows without asking BookMyShow again.
last_output = {}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def fetch_shows_raw(event, date, venue, region):
    r = requests.get(
        "https://in.bookmyshow.com/api/movies-data/seatlayout/v1/primary",
        params={"eventCode": event, "dateCode": date, "regionCode": region, "venueCode": venue},
        headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def fetch_shows(event, date, venue, region):
    data = fetch_shows_raw(event, date, venue, region)
    # Asking for a date with no shows can return another date's shows; keep exact matches only.
    shows = [s for s in data.get("showTimes") or [] if s.get("showDateCode") == date]
    return (data.get("eventData", {}).get("eventTitle") or "",
            data.get("venue", {}).get("venueName") or "", shows)


def fetch_seats(venue, session_id):
    """Return {category: {seat_id: is_available}} for one show."""
    form = {"strCommand": "GETSEATLAYOUT", "strVenueCode": venue, "strParam1": session_id,
            "strAppCode": "WEB", "strParam2": "WEB", "strFormat": "json", "strParam5": "Y",
            "strParam6": "N", "strParam7": "N", "lngTransactionIdentifier": "0"}
    r = requests.post("https://services-in.bookmyshow.com/doTrans.aspx", headers=HEADERS,
                      files={k: (None, v) for k, v in form.items()}, timeout=30)
    r.raise_for_status()
    body = r.json()["BookMyShow"]
    if body.get("blnSuccess") != "true":
        raise RuntimeError(f"seat layout refused: {body.get('strException')}")
    raw = base64.b64decode(body["strData"])
    dec = Cipher(algorithms.AES(KEY), modes.CBC(b"\0" * 16)).decryptor()
    plain = dec.update(raw) + dec.finalize()
    plain = plain[:-plain[-1]].decode()

    cat_part, row_part = plain.split("||", 1)
    cat_names = {}
    for c in cat_part.split("|"):
        f = c.split(":")
        if len(f) >= 2:
            cat_names[f[1]] = f[0]
    seats = {}
    for row in row_part.split("|"):
        f = row.split(":")
        if len(f) < 4 or not f[1]:
            continue
        label = f[1]
        for tok in f[3:]:
            if len(tok) < 2 or "+" not in tok or tok[1] == "0":
                continue
            cat = cat_names.get(tok[0], tok[0])
            seats.setdefault(cat, {})[f"{label}{tok.split('+', 1)[1]}"] = tok[1] in AVAILABLE
    return seats


def summarise_rows(seat_ids):
    rows = {}
    for s in seat_ids:
        m = re.match(r"([A-Za-z]+)", s)
        rows[m.group(1) if m else "?"] = rows.get(m.group(1) if m else "?", 0) + 1
    return " ".join(f"{r}({n})" for r, n in sorted(rows.items()))


def seat_link(event, date, venue, session_id, city):
    """Direct link to one show's seat map; opens the BMS app on phones that have it."""
    return f"https://in.bookmyshow.com/movies/{city}/seat-layout/{event}/{venue}/{session_id}/{date}"


def seat_pairs(seat_ids):
    """Non-overlapping side-by-side available pairs as (label, seat_a, seat_b), front rows first."""
    by_row = {}
    for s in seat_ids:
        m = re.fullmatch(r"([A-Za-z]+)(\d+)", s)
        if m:
            by_row.setdefault(m.group(1), set()).add(int(m.group(2)))
    pairs = []
    for row in sorted(by_row):
        nums, taken = by_row[row], set()
        for n in sorted(nums):
            if n not in taken and n + 1 in nums:
                taken |= {n, n + 1}
                pairs.append((f"{row}{n}-{n + 1}", f"{row}{n}", f"{row}{n + 1}"))
    return pairs


def venue_events(venue, date, city, movie):
    """Event codes (one per format: 2D, 3D, IMAX...) of a movie listed at a venue on a date."""
    r = requests.get(f"https://in.bookmyshow.com/cinemas/{city}/x/buytickets/{venue}/{date}",
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    # Listings link /movies/<city>/<slug>/ET...; the city-wide "Now Showing" footer
    # links /movies/<slug>/ET... and is deliberately not matched.
    found = re.findall(rf"/movies/{re.escape(city)}/([a-z0-9-]+)/(ET\d+)", r.text)
    return sorted({ev for slug, ev in found if movie in slug})


screen_cache = {}


def screens(venue, date, region, city, movie, screen_re):
    """Shows of `movie` at `venue` whose screen/format attributes match `screen_re`."""
    key = (venue, date, city, movie, screen_re.pattern)
    hit = screen_cache.get(key)
    if hit and time.time() - hit[0] < MIN_POLL_GAP:
        return hit[1]
    shows = []
    for ev in venue_events(venue, date, city, movie):
        data = fetch_shows_raw(ev, date, venue, region)
        ed = data.get("eventData", {})
        name = ed.get("eventName") or ev
        for sh in data.get("showTimes") or []:
            attrs = sh.get("attributes") or ""
            # Premium screens show up either as the show's attributes ("PCX Screen",
            # "DOLBY CINEMA") or as a separate format of the movie ("Scene (HDR By Barco)").
            if sh.get("showDateCode") == date and screen_re.search(
                    f"{attrs} | {ed.get('eventDimension') or ''} | {name}"):
                shows.append((sh.get("showTimeCode", ""), sh.get("showTime", "?"), attrs, name,
                              seat_link(ev, date, venue, sh["sessionId"], city)))
        time.sleep(1)
    shows.sort()
    screen_cache[key] = (time.time(), shows)
    return shows


def render_screens(shows, venue, date, movie, screen_re):
    out = [f"{movie} at {venue} on {date}, screens matching /{screen_re.pattern}/", ""]
    # Only show identity goes here (no seat counts), so the text changes only when a
    # matching show is added or removed.
    out += [f"{t} | {attrs} | {name} | Book: {link}" for _, t, attrs, name, link in shows]
    if not shows:
        out.append("No matching shows yet.")
    return "\n".join(out) + "\n"


lang_cache = {}


def lang_open(events, language, dates, city):
    """Per date: theatres (and formats) where `language` is bookable for any of `events`
    (one event code per format, e.g. 2D and HDR) in `city`."""
    key = (tuple(events), language, tuple(dates), city)
    hit = lang_cache.get(key)
    if hit and time.time() - hit[0] < MIN_POLL_GAP:
        return hit[1]
    result = []
    for d in dates:
        venues, fmts, url = set(), set(), None
        for event in events:
            u = f"https://in.bookmyshow.com/movies/{city}/x/buytickets/{event}/{d}"
            r = requests.get(u, headers=HEADERS, timeout=30)
            r.raise_for_status()
            time.sleep(1)
            # A date without shows silently falls back to the first date that has some,
            # so the page only counts when it is really about date d.
            if d not in set(re.findall(r'"showDateCode":"(\d{8})"', r.text)):
                continue
            found = set(re.findall(rf"/cinemas/{re.escape(city)}/([a-z0-9-]+)/buytickets/\w+/", r.text))
            if found:
                venues |= found
                fmts |= {f.split("•", 1)[1].strip() for f in
                         re.findall(rf'"format":"({re.escape(language)} • [^"]+)"', r.text)}
                url = url or u
        result.append((d, sorted(venues), sorted(fmts), url))
    lang_cache[key] = (time.time(), result)
    return result


def render_lang(result, events, language, city):
    out = [f"{','.join(events)} {language} bookings",
           f"BMS page: https://in.bookmyshow.com/movies/{city}/x/{events[0]}", ""]
    # Theatre names and formats only (no seat counts), so the text changes only when
    # a date opens or a theatre/format is added.
    for d, venues, fmts, url in result:
        if venues:
            out.append(f"{d}: {language} BOOKINGS OPEN at {', '.join(venues)}"
                       f" ({'; '.join(fmts) or '?'}) | Book: {url}")
        else:
            out.append(f"{d}: {language} not open yet")
    return "\n".join(out) + "\n"


def event_formats(event, city):
    """Language/format options in the movie page's "Book tickets" picker, one event code each."""
    r = requests.get(f"https://in.bookmyshow.com/movies/{city}/x/{event}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    title = (re.findall(r'"meta":\{"title":"([^"]+)","subtitle":"Select language and format"', r.text) or [event])[0]
    opts = set()
    for lang, fmts in re.findall(r'\{"language":"([^"]+)","formats":\[(.*?)\]\}', r.text):
        for dim, code in re.findall(r'"dimension":"([^"]+)","eventCode":"(ET\d+)"', fmts):
            opts.add((lang, dim, code))
    return title, sorted(opts)


def district_state(page, query=""):
    r = requests.get(f"https://www.district.in/movies/{page}{query}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("District page has no __NEXT_DATA__")
    return json.loads(m.group(1))["props"]["pageProps"]["data"]["serverState"]


def district_lang(page, language, date=""):
    """Language/format groups with shows on a District movie page
    (https://www.district.in/movies/<page>). language may be "Any"; with date
    (YYYY-MM-DD) only groups that have shows on that date count.
    Returns (title, [language, ...])."""
    key = ("district", page, language, date)
    hit = lang_cache.get(key)
    if hit and time.time() - hit[0] < MIN_POLL_GAP:
        return hit[1]
    state = district_state(page)
    groups, title = [], ""
    for movie in state.get("movieContents", {}).values():
        title = title or movie.get("name") or movie.get("title") or ""
        for g in movie.get("languageFormatGroups") or []:
            lang = g.get("lang") or g.get("language") or "?"
            if language != "Any" and lang.lower() != language.lower():
                continue
            if not g.get("number_of_sessions", 1):
                continue
            if date:
                gid = g.get("fmtGrpId") or g.get("format") or ""
                st = district_state(page, f"?frmtid={gid}")
                dates = {d for ms in st.get("movieSessions", {}).values()
                         for d in (ms.get("data") or {}).get("sessionDates") or []}
                time.sleep(1)
                if date not in dates:
                    continue
            groups.append(lang)
    result = (title, sorted(set(groups)))
    lang_cache[key] = (time.time(), result)
    return result


late_cache = {}


def bms_late_shows(event, date, region, city, start):
    """All shows of `event` in `city` on `date` (YYYYMMDD) starting at or after `start` (HHMM).
    Theatres come from the event's buytickets page; showtimes from each theatre's seat-layout data."""
    key = ("bms", event, date, city, start)
    hit = late_cache.get(key)
    if hit and time.time() - hit[0] < MIN_POLL_GAP:
        return hit[1]
    r = requests.get(f"https://in.bookmyshow.com/movies/{city}/x/buytickets/{event}/{date}",
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    venues = sorted(set(re.findall(rf"/cinemas/{re.escape(city)}/[a-z0-9-]+/buytickets/(\w+)/", r.text)))
    shows = []
    for venue in venues:
        try:
            _, venue_name, vshows = fetch_shows(event, date, venue, region)
        except Exception as e:  # one theatre failing shouldn't hide the others
            print(f"late {event}/{date}/{venue}: {e!r}", flush=True)
            continue
        for sh in vshows:
            code = sh.get("showTimeCode", "")
            if code >= start:
                shows.append((code, sh.get("showTime", "?"), venue_name or venue,
                              sh.get("attributes") or "",
                              seat_link(event, date, venue, sh["sessionId"], city)))
        time.sleep(1)
    shows.sort()
    late_cache[key] = (time.time(), shows)
    return shows


def district_late_shows(page, date, start):
    """All District sessions on `date` (YYYY-MM-DD) starting at or after `start` (HHMM)."""
    key = ("district", page, date, start)
    hit = late_cache.get(key)
    if hit and time.time() - hit[0] < MIN_POLL_GAP:
        return hit[1]
    shows = []
    for movie in district_state(page).get("movieContents", {}).values():
        for g in movie.get("languageFormatGroups") or []:
            gid = g.get("fmtGrpId") or g.get("format") or ""
            st = district_state(page, f"?frmtid={gid}&fromdate={date}")
            time.sleep(1)
            pd = ((st.get("movieSessions") or {}).get(f"{gid}{date}") or {}).get("pageData") or {}
            for c in (pd.get("nearbyCinemas") or []) + (pd.get("farCinemas") or []):
                name = (c.get("cinemaInfo") or {}).get("name") or str(c.get("id"))
                for s in c.get("sessions") or []:
                    when = s.get("showTime") or ""  # e.g. 2026-11-18T20:15
                    if not when.startswith(date):
                        continue
                    hhmm = when[11:16].replace(":", "")
                    if hhmm >= start:
                        shows.append((hhmm, datetime.strptime(hhmm, "%H%M").strftime("%I:%M %p"),
                                      name, f"{g.get('lang') or ''} {s.get('scrnFmt') or ''}".strip(),
                                      f"https://www.district.in/movies/{page}?frmtid={gid}&fromdate={date}"))
    shows = sorted(set(shows))
    late_cache[key] = (time.time(), shows)
    return shows


def render_late(shows, header, start):
    out = [header, f"Shows starting at or after {start[:2]}:{start[2:]}", ""]
    # Identity only (no seat counts) so the text changes only when a late show is added/removed.
    out += [f"{t} | {venue} | {fmt} | Book: {link}" for _, t, venue, fmt, link in shows]
    if not shows:
        out.append(f"No shows from {start[:2]}:{start[2:]} yet.")
    return "\n".join(out) + "\n"


def poll(event, date, venue, region, min_seats, city):
    key = f"{event}/{date}/{venue}"
    state = load_state()
    st = state.setdefault(key, {"sessions": {}, "events": [], "initialized": False})
    now = time.time()
    if now - st.get("last_poll", 0) < MIN_POLL_GAP:
        return st

    title, venue_name, shows = fetch_shows(event, date, venue, region)
    stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    if title:
        st["title"] = title
    if venue_name:
        st["venue_name"] = venue_name

    for show in shows:
        sid, when = show["sessionId"], show.get("showTime", "?")
        try:
            seats = fetch_seats(venue, sid)
        except Exception as e:  # keep the previous snapshot for this show
            print(f"{key} session {sid}: {e}", flush=True)
            continue
        time.sleep(1)
        old = st["sessions"].get(sid)
        if old is None and st["initialized"]:
            total = sum(len(v) for v in seats.values())
            avail = sum(sum(v.values()) for v in seats.values())
            st["events"].append(f"{stamp} | {when} | NEW SHOW LISTED | {avail}/{total} seats available"
                                f" | Book: {seat_link(event, date, venue, sid, city)}")
        elif old is not None:
            parts, released, pairs = [], 0, []
            for cat, cur in seats.items():
                prev_avail = set(old["avail"].get(cat, []))
                gained = [s for s, ok in cur.items() if ok and s not in prev_avail]
                # Only count seats that existed before as unavailable, not layout changes.
                gained = [s for s in gained if s in set(old["all"].get(cat, []))]
                if gained:
                    released += len(gained)
                    parts.append(f"{cat} rows {summarise_rows(gained)}")
                    # Pairs among everything free in this category, so a released seat
                    # next to an already-free one still counts.
                    pairs += [f"{cat} {label}"
                              for label, a, b in seat_pairs(s for s, ok in cur.items() if ok)
                              if a in gained or b in gained]
            if released >= min_seats:
                total = sum(len(v) for v in seats.values())
                avail = sum(sum(v.values()) for v in seats.values())
                together = (", ".join(pairs[:MAX_PAIRS]) + (" …" if len(pairs) > MAX_PAIRS else "")
                            if pairs else "none side by side")
                st["events"].append(f"{stamp} | {when} | +{released} seats released | "
                                    f"{'; '.join(parts)} | 2 together: {together} | "
                                    f"now {avail}/{total} available | "
                                    f"Book: {seat_link(event, date, venue, sid, city)}")
        st["sessions"][sid] = {
            "time": when,
            "sort": show.get("showTimeCode", ""),
            "checked": stamp,
            "avail": {c: [s for s, ok in v.items() if ok] for c, v in seats.items()},
            "all": {c: list(v) for c, v in seats.items()},
        }

    live = {s["sessionId"] for s in shows}
    st["live"] = sorted(live)
    st["initialized"] = True
    st["events"] = st["events"][-MAX_EVENTS:]
    st["last_poll"] = now
    st["last_poll_text"] = stamp
    save_state(state)
    return st


def live_sessions(st):
    rows = [(v.get("sort", ""), sid, v) for sid, v in st["sessions"].items() if sid in st.get("live", [])]
    return sorted(rows)


def render_releases(st, event, date, venue, min_seats):
    out = [f"{st.get('title') or event} at {st.get('venue_name') or venue} on {date}",
           f"Alert threshold: {min_seats} or more seats becoming available between checks", ""]
    # No list of current shows here: shows dropping off after their cut-off would
    # change the text and trigger a pointless notification. See /status for that.
    out += ["Events:"]
    out += st["events"] or ["(none yet)"]
    return "\n".join(out) + "\n"


def render_status(st, event, date, venue):
    out = [f"{st.get('title') or event} at {st.get('venue_name') or venue} on {date}",
           f"Last checked: {st.get('last_poll_text', 'never')}", ""]
    for _, sid, v in live_sessions(st):
        cats = ", ".join(f"{c} {len(v['avail'].get(c, []))}/{len(v['all'][c])}" for c in v["all"])
        out.append(f"{v['time']} (session {sid}): {cats} available")
    if len(out) == 3:
        out.append("No shows listed yet.")
    out += ["", "Events:"] + (st["events"] or ["(none yet)"])
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        region = q.get("region", ["HYD"])[0]
        city = q.get("city", [CITY_SLUGS.get(region.upper(), region.lower())])[0]
        start = q.get("from", ["2000"])[0].replace(":", "")
        if url.path == "/last":
            hit = last_output.get(q.get("path", [""])[0])
            if not hit:
                return self.reply(404, "No reply cached for that path\n")
            return self.reply(200, json.dumps({"age": round(time.time() - hit[0]), "text": hit[1]}))
        m = re.fullmatch(r"/late/(ET\d+)/(\d{8})/?", url.path)
        if m:
            event, date = m.groups()
            if not re.fullmatch(r"\d{4}", start):
                return self.reply(400, "from must be HHMM or HH:MM\n")
            try:
                with lock:
                    shows = bms_late_shows(event, date, region, city, start)
            except Exception as e:
                print(f"late failed: {e!r}", flush=True)
                return self.reply(503, f"BookMyShow fetch failed: {e}\n")
            return self.reply(200, render_late(
                shows, f"BMS {event} in {city} on {date} | https://in.bookmyshow.com/movies/{city}/x/buytickets/{event}/{date}", start))
        m = re.fullmatch(r"/district-late/([\w-]+)/?", url.path)
        if m:
            page, date = m.group(1), q.get("date", [""])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not re.fullmatch(r"\d{4}", start):
                return self.reply(400, "Need ?date=YYYY-MM-DD and from=HHMM\n")
            try:
                with lock:
                    shows = district_late_shows(page, date, start)
            except Exception as e:
                print(f"district late failed: {e!r}", flush=True)
                return self.reply(503, f"District fetch failed: {e}\n")
            return self.reply(200, render_late(
                shows, f"District {page} on {date} | https://www.district.in/movies/{page}", start))
        m = re.fullmatch(r"/district/([A-Za-z]+)/([\w-]+)/?", url.path)
        if m:
            language, page = m.group(1).capitalize(), m.group(2)
            date = q.get("date", [""])[0]
            if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                return self.reply(400, "date must be YYYY-MM-DD\n")
            try:
                with lock:
                    title, groups = district_lang(page, language, date)
            except Exception as e:
                print(f"district failed: {e!r}", flush=True)
                return self.reply(503, f"District fetch failed: {e}\n")
            link = f"https://www.district.in/movies/{page}"
            # No session counts: they change all the time and would trigger alerts.
            what = f"{language} bookings" + (f" on {date}" if date else "")
            lines = [f"District {title or page} {what}", f"District page: {link}", ""]
            lines += ([f"District: {what} OPEN ({', '.join(groups)}) | Book: {link}"]
                      if groups else [f"District: {what} not open yet"])
            return self.reply(200, "\n".join(lines) + "\n")
        m = re.fullmatch(r"/lang/(ET\d+(?:,ET\d+)*)/([A-Za-z]+)/?", url.path)
        if m:
            event, language = m.group(1).split(","), m.group(2).capitalize()
            dates = [d for d in q.get("dates", [""])[0].split(",") if re.fullmatch(r"\d{8}", d)]
            if not dates:
                return self.reply(400, "Need ?dates=YYYYMMDD[,YYYYMMDD...]\n")
            try:
                with lock:
                    result = lang_open(event, language, dates, city)
            except Exception as e:
                print(f"lang failed: {e!r}", flush=True)
                return self.reply(503, f"BookMyShow fetch failed: {e}\n")
            return self.reply(200, render_lang(result, event, language, city))
        m = re.fullmatch(r"/formats/(ET\d+)/?", url.path)
        if m:
            try:
                with lock:
                    title, opts = event_formats(m.group(1), city)
            except Exception as e:
                print(f"formats failed: {e!r}", flush=True)
                return self.reply(503, f"BookMyShow fetch failed: {e}\n")
            page = f"https://in.bookmyshow.com/movies/{city}/x/{m.group(1)}"
            lines = [f"{title} bookable languages/formats on BMS", f"BMS page: {page}", ""]
            lines += [f"{lang} {dim} BOOKABLE ({code}) | Book: https://in.bookmyshow.com/movies/{city}/x/buytickets/{code}"
                      for lang, dim, code in opts] or ["No bookable formats yet"]
            return self.reply(200, "\n".join(lines) + "\n")
        m = re.fullmatch(r"/screens/(\w+)/(\d{8}(?:,\d{8})*)/?", url.path)
        if m:
            venue, dates = m.group(1), m.group(2).split(",")
            if "movie" not in q or "screen" not in q:
                return self.reply(400, "Need ?movie=<slug part>&screen=<regex on screen format>\n")
            movie = q["movie"][0].lower()
            screen_re = re.compile(q["screen"][0], re.I)
            try:
                with lock:
                    # Several dates: one block per date, each exactly as a single-date call renders it.
                    text = "\n".join(render_screens(screens(venue, d, region, city, movie, screen_re),
                                                    venue, d, movie, screen_re) for d in dates)
            except Exception as e:
                print(f"screens failed: {e!r}", flush=True)
                return self.reply(503, f"BookMyShow fetch failed: {e}\n")
            return self.reply(200, text)
        m = re.fullmatch(r"/(releases|status)/(ET\d+)/(\d{8})/(\w+)/?", url.path)
        if not m:
            return self.reply(404, "Use /releases/<eventCode>/<YYYYMMDD>/<venueCode>, /status/... "
                                   "or /screens/<venueCode>/<YYYYMMDD>?movie=..&screen=..\n")
        kind, event, date, venue = m.groups()
        min_seats = int(q.get("min", [DEFAULT_MIN])[0])
        try:
            with lock:
                st = poll(event, date, venue, region, min_seats, city)
        except Exception as e:
            print(f"poll failed: {e!r}", flush=True)
            return self.reply(503, f"BookMyShow fetch failed: {e}\n")
        text = (render_releases(st, event, date, venue, min_seats) if kind == "releases"
                else render_status(st, event, date, venue))
        self.reply(200, text)

    def reply(self, code, text):
        if code == 200 and not self.path.startswith("/last"):
            last_output[self.path] = (time.time(), text)
            if len(last_output) > 500:
                last_output.pop(next(iter(last_output)))
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
