#!/usr/bin/env python3
"""
Roblox Top-100 Trending Tracker
-------------------------------
Runs every few minutes (GitHub Actions). Each run it:
  1. Pulls Roblox's "Top Trending" sort (top 100 experiences) + live player counts
  2. Spots games that are NEW to the top 100
  3. Alerts when a new game's player count grows massively in a short time
  4. Counts how many other Roblox games have a similar name
  5. Writes data.json for the dashboard and sends Discord / email / phone alerts

Standard library only - no pip install needed.

Usage:
  python tracker.py              normal run
  python tracker.py --dry-run    run but don't send alerts
  python tracker.py --test-alert send one sample alert to every channel
"""
import json
import os
import re
import smtplib
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from difflib import SequenceMatcher
from email.message import EmailMessage


def env(key, default):
    v = os.environ.get(key)
    return default if v in (None, "") else type(default)(v)


# ---------------------------------------------------------------- settings
DATA_DIR = env("DATA_DIR", "data")
SORT_ID = env("SORT_ID", "top-trending")          # or top-playing-now / up-and-coming
TOP_N = env("TOP_N", 100)
MIN_PLAYERS = env("MIN_PLAYERS", 1000)            # ignore games smaller than this
SPIKE_PCT = env("SPIKE_PCT", 50.0)                # % growth inside the window...
SPIKE_WINDOW_MIN = env("SPIKE_WINDOW_MIN", 60)    # ...measured over this many minutes
SPIKE_MIN_GAIN = env("SPIKE_MIN_GAIN", 500)       # and at least this many extra players
WATCH_HOURS = env("WATCH_HOURS", 24.0)            # a game counts as "new" this long after entering
NEW_LOOKBACK_DAYS = env("NEW_LOOKBACK_DAYS", 7.0) # not in the top 100 for this long = new
ALERT_COOLDOWN_HOURS = env("ALERT_COOLDOWN_HOURS", 6.0)
HISTORY_HOURS = env("HISTORY_HOURS", 24.0)
NEW_LIST_MAX_AGE_DAYS = env("NEW_LIST_MAX_AGE_DAYS", 30.0)  # age limit for the "new & most played" list
ENTRY_ALERT_MAX_AGE_DAYS = env("ENTRY_ALERT_MAX_AGE_DAYS", 10.0)  # alert as soon as a game this young enters a list (0 = off)
MAX_AGE_DAYS = env("MAX_AGE_DAYS", 30.0)          # only alert for games created this recently (0 = off)
SIMILAR_THRESHOLD = env("SIMILAR_THRESHOLD", 0.6)
SEARCH_PAGES = env("SEARCH_PAGES", 3)
SIMILAR_LOOKUPS_PER_RUN = env("SIMILAR_LOOKUPS_PER_RUN", 4)  # extra lookups for the dashboard (alerts always get one)
_lookups = {"left": 0}             # ~40 results per page
DASHBOARD_URL = env("DASHBOARD_URL", "")

UA = "Mozilla/5.0 (compatible; RobloxTrendTracker/1.0)"
SESSION = str(uuid.uuid4())
NOW = time.time()


# ---------------------------------------------------------------- http
def http(url, data=None, headers=None, tries=4):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
                return json.loads(body) if body and body[:1] in b"{[" else body
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * (i + 1) * (2 if e.code == 429 else 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"Request kept failing: {url}")


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- roblox
def fetch_top(sort_id=SORT_ID, device="computer", country="all", tries=4):
    """Top N games of the chosen sort, in rank order (sponsored slots removed)."""
    games, token, seen = [], None, set()
    for _ in range(5):
        q = {"sessionId": SESSION, "sortId": sort_id, "device": device, "country": country}
        if token:
            q["pageToken"] = token
        res = http("https://apis.roblox.com/explore-api/v1/get-sort-content?" + urllib.parse.urlencode(q), tries=tries)
        for g in res.get("games", []):
            uid = g.get("universeId")
            if not uid or g.get("isSponsored") or uid in seen:
                continue
            seen.add(uid)
            games.append(g)
        token = res.get("nextPageToken")
        if not token or len(games) >= TOP_N:
            break
    return games[:TOP_N]


POOL_SORTS = ["top-playing-now", "top-trending", "up-and-coming"]
POOL_DEVICES = ["computer", "phone"]
POOL_COUNTRIES = env("POOL_COUNTRIES", "us,gb,br,ph,de").split(",")
WIDE_SCAN_MINUTES = env("WIDE_SCAN_MINUTES", 30)   # how often to re-scan other countries/devices
WIDE_SCAN_BUDGET_SEC = env("WIDE_SCAN_BUDGET_SEC", 60)


def walk_games(obj, out):
    """Collect every game-like object anywhere in a JSON response."""
    if isinstance(obj, dict):
        if obj.get("universeId") and obj.get("name") and not obj.get("isSponsored"):
            out.setdefault(obj["universeId"], obj)
        for v in obj.values():
            walk_games(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_games(v, out)


def fetch_new_played(st):
    """Most-played games created in the last NEW_LIST_MAX_AGE_DAYS days.
    Roblox's lists are only ~100 deep, so pool every list on the home page,
    keep the young games and rank them by live players."""
    pool = {}
    cache = st.setdefault("pool", {"refreshedAt": 0, "ids": []})
    try:
        res = http("https://apis.roblox.com/explore-api/v1/get-sorts?" + urllib.parse.urlencode(
            {"sessionId": SESSION, "device": "computer", "country": "all"}))
        walk_games(res, pool)
    except Exception as e:
        print("home page sorts failed:", e)
    for sid in POOL_SORTS:  # main lists, every run
        try:
            for g in fetch_top(sid):
                pool.setdefault(g["universeId"], g)
        except Exception as e:
            print(f"sort {sid} failed:", e)
    if NOW - cache["refreshedAt"] > WIDE_SCAN_MINUTES * 60:  # wider scan, every 30 min, time-limited
        start, n = time.time(), 0
        for sid in POOL_SORTS:
            for device in POOL_DEVICES:
                for country in ["all"] + [c.strip() for c in POOL_COUNTRIES if c.strip()]:
                    if (device, country) == ("computer", "all") or time.time() - start > WIDE_SCAN_BUDGET_SEC:
                        continue
                    try:
                        for g in fetch_top(sid, device, country, tries=1):
                            pool.setdefault(g["universeId"], g)
                        n += 1
                    except Exception as e:
                        print(f"wide scan {sid}/{device}/{country} skipped:", e)
                    time.sleep(0.5)
        cache["refreshedAt"] = NOW
        print(f"wide scan: {n} lists in {time.time() - start:.0f}s")
    for uid in cache["ids"]:  # young games found by earlier scans: re-check their live players
        pool.setdefault(uid, {"universeId": uid})
    games = enrich(list(pool.values()), icons=False)
    young = [g for g in games if (age_days(g) is not None and age_days(g) <= NEW_LIST_MAX_AGE_DAYS)]
    young.sort(key=lambda g: -g["players"])
    cache["ids"] = [g["universeId"] for g in young[:500]]
    young = young[:TOP_N]
    add_icons(young)
    for i, g in enumerate(young, 1):
        g["rank"] = i
    print(f"new & most played: {len(pool)} games pooled, {len(young)} created in the last {NEW_LIST_MAX_AGE_DAYS:g} days")
    return young


def add_icons(games):
    icons = {}
    for part in chunks([g["universeId"] for g in games], 50):
        try:
            res = http("https://thumbnails.roblox.com/v1/games/icons?" + urllib.parse.urlencode({
                "universeIds": ",".join(map(str, part)), "size": "150x150", "format": "Png",
                "isCircular": "false", "returnPolicy": "PlaceHolder"}))
            for d in res.get("data", []):
                icons[d["targetId"]] = d.get("imageUrl")
        except Exception as e:
            print("icon lookup failed:", e)
    for g in games:
        g["icon"] = icons.get(g["universeId"])


def enrich(games, icons=True):
    """Add exact live player count, visits, creator, created date and icon."""
    ids = [g["universeId"] for g in games]
    details = {}
    for part in chunks(ids, 50):
        try:
            res = http("https://games.roblox.com/v1/games?universeIds=" + ",".join(map(str, part)))
            for d in res.get("data", []):
                details[d["id"]] = d
        except Exception as e:
            print("details lookup failed:", e)
    out = []
    for rank, g in enumerate(games, 1):
        d = details.get(g["universeId"], {})
        out.append({
            "rank": rank,
            "universeId": g["universeId"],
            "placeId": g.get("rootPlaceId") or d.get("rootPlaceId"),
            "name": g.get("name") or d.get("name", "?"),
            "players": int(d.get("playing", g.get("playerCount", 0)) or 0),
            "visits": d.get("visits"),
            "created": d.get("created"),
            "creator": (d.get("creator") or {}).get("name"),
            "genre": g.get("genreL1"),
            "icon": None,
            "likes": g.get("totalUpVotes"),
            "dislikes": g.get("totalDownVotes"),
        })
    if icons:
        add_icons(out)
    return out


# ---------------------------------------------------------------- similar names
STOPWORDS = {"the", "a", "an", "of", "and", "update", "upd", "new", "beta", "alpha",
             "release", "official", "free", "event", "sale", "x2", "2x", "release"}


def norm(name):
    s = unicodedata.normalize("NFKD", name or "")
    s = re.sub(r"[\[\(\{【].*?[\]\)\}】]", " ", s)   # drop tags like [UPDATE] (NEW!)
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())       # drop emojis / punctuation
    return " ".join(w for w in s.split() if w not in STOPWORDS)


def similarity(a, b):
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb)
    seq = SequenceMatcher(None, a, b).ratio()
    contains = 0.9 if (len(a) >= 5 and a in b) or (len(b) >= 5 and b in a) else 0.0
    short = ta if len(ta) <= len(tb) else tb   # every word of the shorter name appears in the other
    if len("".join(short)) >= 3 and short <= (ta | tb) and short <= ta & tb:
        contains = max(contains, 0.85)
    return max(jaccard, seq, contains)


def find_similar(game, top_games):
    """Search Roblox for the game's name and count games whose names look alike."""
    target = norm(game["name"])
    if not target:
        return {"count": None, "scanned": 0, "inTop": 0, "examples": [], "note": "name has no searchable letters"}
    found, token, pages_ok = {}, None, 0
    for page in range(SEARCH_PAGES):
        q = {"searchQuery": target, "sessionId": SESSION, "pageType": "all"}
        if token:
            q["pageToken"] = token
            time.sleep(2)  # Roblox rate-limits rapid search paging
        try:
            res = http("https://apis.roblox.com/search-api/omni-search?" + urllib.parse.urlencode(q), tries=2)
        except Exception as e:
            if page == 0:  # first page blocked: wait and try once more
                time.sleep(12)
                try:
                    res = http("https://apis.roblox.com/search-api/omni-search?" + urllib.parse.urlencode(q), tries=1)
                except Exception:
                    print("search failed:", e)
                    break
            else:
                print("search failed:", e)
                break
        pages_ok += 1
        for group in res.get("searchResults", []):
            for c in group.get("contents", []) or []:
                uid = c.get("universeId")
                if uid and uid != game["universeId"]:
                    found[uid] = c
        token = res.get("nextPageToken")
        if not token:
            break
    if not pages_ok:
        return {"count": None, "scanned": 0, "inTop": 0, "examples": [], "checkedAt": NOW - 5 * 3600,
                "note": "Roblox search was busy - try the game name in Roblox search"}
    matches = []
    for uid, c in found.items():
        score = similarity(target, norm(c.get("name")))
        if score >= SIMILAR_THRESHOLD:
            matches.append({"universeId": uid, "placeId": c.get("rootPlaceId"), "name": c.get("name"),
                            "players": c.get("playerCount", 0), "score": round(score, 2)})
    matches.sort(key=lambda m: (-m["players"], -m["score"]))
    in_top = sum(1 for t in top_games if t["universeId"] != game["universeId"]
                 and similarity(target, norm(t["name"])) >= SIMILAR_THRESHOLD)
    return {"count": len(matches), "scanned": len(found), "inTop": in_top,
            "examples": matches[:5], "checkedAt": NOW}


# ---------------------------------------------------------------- growth maths
def growth(hist, since_ts):
    """Growth from the oldest sample at/after since_ts to the latest sample."""
    pts = [p for p in hist if p[0] >= since_ts]
    if len(pts) < 2:
        return None
    a, b = pts[0][1], pts[-1][1]
    return {"from": a, "to": b, "gain": b - a, "pct": round((b - a) / a * 100, 1) if a else None,
            "minutes": round((pts[-1][0] - pts[0][0]) / 60)}


def dur(sec):
    m = max(0, round(sec / 60))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{h // 24} days {h % 24}h"


def surge_start(hist, since_ts):
    """When the climb began: the lowest player count since since_ts (before the latest sample)."""
    pts = [p for p in hist[:-1] if p[0] >= since_ts]
    return min(pts, key=lambda p: (p[1], -p[0]))[0] if pts else None


def timing_text(entered, surge):
    """'In the list for ...' and 'surge started ... after entering' lines."""
    lines = []
    if entered:
        lines.append(f"In the list for {dur(NOW - entered)}" if NOW - entered >= 60 else "Just entered the list")
    if entered and surge:
        gap = surge - entered
        when = "right after entering" if gap < 60 else f"{dur(gap)} after entering"
        lines.append(f"Surge started {when} ({dur(NOW - surge)} ago)")
    return lines


def is_spike(game, hist, entered_at):
    """Returns (reason, surge_start_ts) or (None, None)."""
    if game["players"] < MIN_PLAYERS:
        return None, None
    win_start = NOW - SPIKE_WINDOW_MIN * 60 - 90
    w = growth(hist, win_start)
    if w and w["gain"] >= SPIKE_MIN_GAIN and w["pct"] is not None and w["pct"] >= SPIKE_PCT:
        return (f"+{w['pct']:.0f}% in {w['minutes']} min ({w['from']:,} → {w['to']:,})",
                surge_start(hist, max(win_start, entered_at - 1)))
    e = growth(hist, entered_at - 1)
    if e and e["from"] and e["to"] >= 2 * e["from"] and e["gain"] >= SPIKE_MIN_GAIN:
        return (f"{e['to'] / e['from']:.1f}× since entering the list ({e['from']:,} → {e['to']:,})",
                surge_start(hist, entered_at - 1))
    return None, None
    if game["players"] < MIN_PLAYERS:
        return None
    w = growth(hist, NOW - SPIKE_WINDOW_MIN * 60 - 90)
    if w and w["gain"] >= SPIKE_MIN_GAIN and w["pct"] is not None and w["pct"] >= SPIKE_PCT:
        return f"+{w['pct']:.0f}% in {w['minutes']} min ({w['from']:,} → {w['to']:,})"
    e = growth(hist, entered_at - 1)
    if e and e["from"] and e["to"] >= 2 * e["from"] and e["gain"] >= SPIKE_MIN_GAIN:
        return f"{e['to'] / e['from']:.1f}× since entering the list ({e['from']:,} → {e['to']:,})"
    return None


def age_days(game):
    """Days since the game was first created/uploaded, or None if unknown."""
    try:
        from datetime import datetime
        c = datetime.fromisoformat(game["created"].replace("Z", "+00:00"))
        return (NOW - c.timestamp()) / 86400
    except Exception:
        return None


def young_enough(game):
    if MAX_AGE_DAYS <= 0:
        return True
    age = age_days(game)
    return age is not None and age <= MAX_AGE_DAYS


# ---------------------------------------------------------------- alerts
def game_url(g):
    return f"https://www.roblox.com/games/{g.get('placeId') or ''}"


def alert_text(a):
    s = a["similar"]
    sim = (f"unknown ({s.get('note', 'name not searchable')})" if s.get("count") is None else
           f"{s['count']} similar names found (out of {s['scanned']} search results), "
           f"{s['inTop']} also in this top list")
    kind = a.get("kind")
    first = {"entry": f"{a['name']} just entered the list!",
             "trend": f"{a['name']} is blowing up on Steam - no big Roblox game yet!"}.get(kind, f"{a['name']} is blowing up!")
    lines = [first,
             f"{'Growth' if kind == 'spike' else 'Why'}: {a['reason']}",
             f"{'Steam players' if kind == 'trend' else 'Players'} now: {a['players']:,}  |  Rank #{a['rank']} in {a.get('listTitle', SORT_ID)}",
             f"{'Released' if kind == 'trend' else 'Uploaded'}: {a['ageText']}",
             *a.get("timing", []),
             f"Similar names: {sim}"]
    if s.get("examples"):
        lines.append("Look-alikes: " + "; ".join(f"{x['name']} ({x['players']:,})" for x in s["examples"][:3]))
    lines.append(a["url"])
    return "\n".join(lines)


def send_discord(a):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    s = a["similar"]
    fields = [
        {"name": "Growth" if a.get("kind") == "spike" else "Why", "value": a["reason"], "inline": False},
        {"name": "Steam players" if a.get("kind") == "trend" else "Players now", "value": f"{a['players']:,}", "inline": True},
        {"name": "Rank", "value": f"#{a['rank']} – {a.get('listTitle', '')}", "inline": True},
        {"name": "Released" if a.get("kind") == "trend" else "Uploaded", "value": a["ageText"], "inline": True},
        *([{"name": "Timing", "value": "\n".join(a["timing"]), "inline": False}] if a.get("timing") else []),
        {"name": "Similar names", "value": "?" if s.get("count") is None else
         f"**{s['count']}** found ({s['inTop']} in this list)", "inline": True},
    ]
    if s.get("examples"):
        fields.append({"name": "Look-alikes", "inline": False, "value": "\n".join(
            f"[{x['name'][:60]}](https://www.roblox.com/games/{x['placeId']}) – {x['players']:,} playing"
            for x in s["examples"][:5])[:1000]})
    entry = a.get("kind") == "entry"
    trend = a.get("kind") == "trend"
    embed = {"title": f"{'🔥' if trend else '🆕' if entry else '🚀'} {a['name']}"[:250], "url": a["url"],
             "color": 0xFF9F1C if trend else 0x2ECC71 if entry else 0xFF4D4D,
             "fields": fields, "footer": {"text": "Roblox Trend Tracker"},
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(a["time"]))}
    if a.get("icon"):
        embed["thumbnail"] = {"url": a["icon"]}
    if DASHBOARD_URL:
        embed["description"] = f"[Open dashboard]({DASHBOARD_URL})"
    http(url, json.dumps({"username": "Trend Tracker", "embeds": [embed]}).encode(),
         {"Content-Type": "application/json"})


def send_ntfy(a):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    server = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    entry = a.get("kind") == "entry"
    title = {"entry": f"New game entered: {a['name']}", "trend": f"Trend idea (no Roblox game yet): {a['name']}"}.get(
        a.get("kind"), f"Trending spike: {a['name']}")
    body = {"topic": topic, "title": title[:200],
            "message": alert_text(a), "click": a["url"], "tags": ["new" if entry else "rocket"], "priority": 4}
    if a.get("icon"):
        body["icon"] = a["icon"]
    topic_len = len(topic)
    res = http(server, json.dumps(body).encode(), {"Content-Type": "application/json"})
    print(f"ntfy sent (topic is {topic_len} characters, starts with '{topic[:2]}'):",
          res.get("id") if isinstance(res, dict) else res)


def send_email(a):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    if not host or not to:
        return
    port = env("SMTP_PORT", 587)
    user, pw = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", "")
    msg = EmailMessage()
    msg["Subject"] = (f"🔥 Trend idea, no Roblox game yet: {a['name']}" if a.get("kind") == "trend"
                      else f"🆕 New Roblox game entered the list: {a['name']}" if a.get("kind") == "entry"
                      else f"🚀 Roblox trending spike: {a['name']}")
    msg["From"] = env("EMAIL_FROM", user)
    msg["To"] = to
    msg.set_content(alert_text(a) + (f"\n\nDashboard: {DASHBOARD_URL}" if DASHBOARD_URL else ""))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.send_message(msg)


def send_all(a):
    print("Alert channels set up:",
          {"discord": bool(os.environ.get("DISCORD_WEBHOOK_URL")), "phone (ntfy)": bool(os.environ.get("NTFY_TOPIC")),
           "email": bool(os.environ.get("SMTP_HOST") and os.environ.get("EMAIL_TO"))})
    for fn in (send_discord, send_ntfy, send_email):
        try:
            fn(a)
        except Exception as e:
            print(f"{fn.__name__} failed: {e}")


# ---------------------------------------------------------------- state
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def age_text(g):
    d = age_days(g)
    if d is None:
        return "unknown"
    return f"{d * 24:.0f} hours ago" if d < 2 else f"{d:.0f} days ago"


def build_alert(g, reason, similar):
    return {"time": NOW, "universeId": g["universeId"], "name": g["name"], "players": g["players"],
            "rank": g["rank"], "reason": reason, "url": game_url(g), "icon": g.get("icon"),
            "similar": similar, "ageText": age_text(g), "kind": "spike"}


STATE = {}
LISTS = [
    ("trending", "Top Trending", lambda: enrich(fetch_top())),
    ("upcoming", "Up and Coming", lambda: enrich(fetch_top("up-and-coming"))),
    ("newPlayed2", f"New & Most Played (under {NEW_LIST_MAX_AGE_DAYS:g} days)", lambda: fetch_new_played(STATE)),
]


def process_list(key, title, top, ls, last_alert, run_alerts):
    """Update one list's history; return its new spike alerts."""
    for k in ("seen", "history", "similar"):
        ls.setdefault(k, {})
    bootstrap = not ls.get("bootstrapped")
    alerts = []
    for g in top:
        uid = str(g["universeId"])
        seen = ls["seen"].get(uid)
        if bootstrap:  # first ever run: everything already there is not "new"
            seen = {"first": NOW, "last": NOW, "enteredAt": None}
        elif seen is None or NOW - seen["last"] > NEW_LOOKBACK_DAYS * 86400:
            seen = {"first": NOW, "last": NOW, "enteredAt": NOW}
        seen["last"] = NOW
        ls["seen"][uid] = seen
        hist = ls["history"].setdefault(uid, [])
        hist.append([round(NOW), g["players"], g["rank"]])

        entered = seen.get("enteredAt")
        g["isNew"] = bool(entered and NOW - entered <= WATCH_HOURS * 3600)
        g["enteredAt"] = entered
        g["ageDays"] = None if age_days(g) is None else round(age_days(g), 1)
        g["growth1h"] = growth(hist, NOW - 3600 - 90)
        g["growthEntry"] = growth(hist, entered - 1) if entered else None

        age = age_days(g)
        if (not bootstrap and entered == NOW and ENTRY_ALERT_MAX_AGE_DAYS > 0
                and age is not None and age <= ENTRY_ALERT_MAX_AGE_DAYS):
            key_e = uid + ":entry"
            if key_e in run_alerts:
                run_alerts[key_e]["listTitle"] += f" + {title}"
            elif NOW - last_alert.get(key_e, 0) > 7 * 86400:
                a = build_alert(g, f"Uploaded {age_text(g)}, just entered at #{g['rank']}", find_similar(g, top))
                a["kind"], a["list"], a["listTitle"] = "entry", key, title
                a["timing"] = timing_text(entered, None)
                ls["similar"][uid] = a["similar"]
                last_alert[key_e] = NOW
                run_alerts[key_e] = a
                alerts.append(a)

        if g["isNew"]:
            cached = ls["similar"].get(uid)
            if (not cached or NOW - cached.get("checkedAt", 0) > 6 * 3600) and _lookups["left"] > 0:
                _lookups["left"] -= 1
                ls["similar"][uid] = cached = find_similar(g, top)
            g["similar"] = cached
            reason, surge = is_spike(g, hist, entered)
            if reason and not young_enough(g):
                print(f"Skipped alert for {g['name']}: created {g.get('created')} (older than {MAX_AGE_DAYS:g} days)")
                reason = None
            if reason and uid in run_alerts:  # already alerting from another list this run
                run_alerts[uid]["listTitle"] += f" + {title}"
                reason = None
            if reason and NOW - last_alert.get(uid, 0) > ALERT_COOLDOWN_HOURS * 3600:
                a = build_alert(g, reason, find_similar(g, top))  # fresh count for the alert
                a["timing"] = timing_text(entered, surge)
                a["list"], a["listTitle"] = key, title
                ls["similar"][uid] = g["similar"] = a["similar"]
                last_alert[uid] = NOW
                run_alerts[uid] = a
                alerts.append(a)
        else:
            g["similar"] = ls["similar"].get(uid)

    # prune old data
    cutoff = NOW - HISTORY_HOURS * 3600
    for uid in list(ls["history"]):
        ls["history"][uid] = [p for p in ls["history"][uid] if p[0] >= cutoff]
        if not ls["history"][uid]:
            del ls["history"][uid]
    for uid in list(ls["seen"]):
        if NOW - ls["seen"][uid]["last"] > (NEW_LOOKBACK_DAYS + 1) * 86400:
            ls["seen"].pop(uid, None)
    current = {str(g["universeId"]) for g in top}
    for uid in list(ls["similar"]):
        if uid not in current and NOW - ls["similar"][uid].get("checkedAt", 0) > 86400:
            del ls["similar"][uid]
    for g in top:
        g["spark"] = [p[1] for p in ls["history"].get(str(g["universeId"]), [])][-72:]
    ls["bootstrapped"] = True
    print(f"{title}: {len(top)} games, {sum(g['isNew'] for g in top)} new, {len(alerts)} alerts"
          + (" (first run - baseline saved)" if bootstrap else ""))
    return alerts


def main():
    dry = "--dry-run" in sys.argv
    os.makedirs(DATA_DIR, exist_ok=True)
    state_path = os.path.join(DATA_DIR, "state.json")
    global STATE
    st = STATE = load(state_path, {})
    st.setdefault("lists", {})
    st.setdefault("alerts", [])
    st.setdefault("lastAlert", {})   # shared by all lists so a game only alerts once
    if "seen" in st:  # migrate the old single-list state
        st["lists"]["trending"] = {k: st.pop(k) for k in ("seen", "history", "similar", "bootstrapped") if k in st}
    for uid in list(st["lastAlert"]):
        if NOW - st["lastAlert"][uid] > 7 * 86400:
            del st["lastAlert"][uid]

    if "--test-alert" in sys.argv:
        top = enrich(fetch_top())
        g = top[0]
        a = build_alert(g, "TEST ALERT – this is what a real alert looks like", find_similar(g, top))
        a["listTitle"] = LISTS[0][1]
        print(alert_text(a))
        send_all(a)
        return

    lists_out, new_alerts, run_alerts = {}, [], {}
    _lookups["left"] = SIMILAR_LOOKUPS_PER_RUN
    st["lists"].pop("newPlayed", None)  # one-off: restart the new list's baseline with the wider pool
    for key, title, fetch in LISTS:
        try:
            top = fetch()
        except Exception as e:
            print(f"{title} failed: {e}")
            continue
        if not top and key == "trending":
            print("No games returned - Roblox API may have changed or blocked the request.")
            sys.exit(1)
        new_alerts += process_list(key, title, top, st["lists"].setdefault(key, {}), st["lastAlert"], run_alerts)
        lists_out[key] = {"title": title, "games": top}

    st["alerts"] = (new_alerts + st["alerts"])[:200]
    try:  # trend scout: games blowing up outside Roblox (runs every 30 min)
        import trend_scout
        trend_scout.T = sys.modules[__name__]
        trends = trend_scout.scout(st, [g for l in lists_out.values() for g in l["games"]], dry)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("trend scout failed:", e)
        trends = st.get("scoutOut")
    for a in new_alerts:
        print("ALERT:\n" + alert_text(a) + "\n")
        if not dry:
            send_all(a)

    save(state_path, st)
    save(os.path.join(DATA_DIR, "data.json"), {
        "updatedAt": NOW, "topN": TOP_N,
        "settings": {"minPlayers": MIN_PLAYERS, "spikePct": SPIKE_PCT, "spikeWindowMin": SPIKE_WINDOW_MIN,
                     "spikeMinGain": SPIKE_MIN_GAIN, "watchHours": WATCH_HOURS,
                     "maxAgeDays": MAX_AGE_DAYS, "newListMaxAgeDays": NEW_LIST_MAX_AGE_DAYS},
        "lists": lists_out, "alerts": st["alerts"][:100], "trends": trends,
    })


if __name__ == "__main__":
    main()
