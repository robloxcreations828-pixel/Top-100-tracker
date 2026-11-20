"""
Trend Scout - finds games blowing up OUTSIDE Roblox that have no popular Roblox version yet.

Every SCOUT_EVERY_MIN minutes (called from tracker.py):
  1. Steam: most-played top 100, top sellers and new releases, with live player counts
  2. Buzz (optional): Reddit gaming subreddits and YouTube trending gaming videos
  3. A Steam game counts as "blowing up" if it has enough players AND it is growing fast,
     jumped up the most-played chart, or is a recent release in the top sellers
  4. For those, it searches Roblox for look-alike games (competition)
  5. Ranks every idea by demand vs competition, alerts on wide-open ones
"""
import math
import os
import re
import time
import urllib.parse

import tracker as T

SCOUT_VERSION = 2  # bump when the rules change -> re-scan on the next run
SCOUT_EVERY_MIN = T.env("SCOUT_EVERY_MIN", 30)
STEAM_MIN_PLAYERS = T.env("STEAM_MIN_PLAYERS", 3000)
STEAM_GROWTH_PCT = T.env("STEAM_GROWTH_PCT", 50.0)      # 24h growth that counts as blowing up
STEAM_RANK_JUMP = T.env("STEAM_RANK_JUMP", 25)          # places climbed vs last week
NEW_RELEASE_DAYS = T.env("NEW_RELEASE_DAYS", 60)
STEAM_MAX_AGE_DAYS = T.env("STEAM_MAX_AGE_DAYS", 60.0)   # only Steam games released this recently
ROBLOX_OPEN_MAX_PLAYERS = T.env("ROBLOX_OPEN_MAX_PLAYERS", 2000)  # biggest Roblox look-alike below this = "open"
ROBLOX_CHECKS_PER_SCAN = T.env("ROBLOX_CHECKS_PER_SCAN", 6)
TREND_ALERT_COOLDOWN_DAYS = T.env("TREND_ALERT_COOLDOWN_DAYS", 7.0)
REDDIT_SUBS = ["gaming", "pcgaming", "Steam", "IndieGaming", "LivestreamFail"]


def get(url, tries=2):
    return T.http(url, tries=tries)


# ------------------------------------------------------------------ Steam
def steam_candidates():
    cands = {}  # appid -> info

    try:
        res = get("https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/")
        for r in (res.get("response") or {}).get("ranks", [])[:100]:
            cands[r["appid"]] = {"appid": r["appid"], "chartRank": r.get("rank"),
                                 "lastWeekRank": r.get("last_week_rank") or 0,
                                 "peak": r.get("peak_in_game")}
    except Exception as e:
        print("steam most-played failed:", e)

    try:
        res = get("https://store.steampowered.com/api/featuredcategories?cc=us&l=english")
        for key in ("top_sellers", "new_releases"):
            for it in (res.get(key) or {}).get("items", [])[:40]:
                c = cands.setdefault(it["id"], {"appid": it["id"]})
                c.setdefault("name", it.get("name"))
                c.setdefault("image", it.get("header_image") or it.get("large_capsule_image"))
                c[key] = True
    except Exception as e:
        print("steam featured failed:", e)

    # Steam's "Popular New Releases" list (the store search page, as JSON)
    for flt in ("popularnew", "topsellers"):
        try:
            res = get("https://store.steampowered.com/search/results/?" + urllib.parse.urlencode(
                {"filter": flt, "json": 1, "count": 75, "cc": "us", "l": "english", "category1": 998}))
            for it in res.get("items", []):
                m = re.search(r"/apps/(\d+)/", it.get("logo", ""))
                if m:
                    c = cands.setdefault(int(m.group(1)), {"appid": int(m.group(1))})
                    c.setdefault("name", it.get("name"))
                    c["top_sellers" if flt == "topsellers" else "popular_new"] = True
        except Exception as e:
            print(f"steam search {flt} failed:", str(e)[:80])
    return cands


def steam_details(appid, cache):
    """Name, type, image, release date (cached for a week)."""
    c = cache.get(str(appid))
    if c and time.time() - c.get("at", 0) < 7 * 86400:
        return c
    try:
        res = get(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic,release_date&l=english")
        d = (res.get(str(appid)) or {}).get("data") or {}
        c = {"name": d.get("name"), "type": d.get("type"), "image": d.get("header_image"),
             "free": d.get("is_free"), "release": (d.get("release_date") or {}).get("date"), "at": time.time()}
    except Exception as e:
        print(f"steam details {appid} failed:", e)
        c = c or {"at": time.time() - 6 * 86400}
    cache[str(appid)] = c
    return c


def steam_players(appid):
    try:
        res = get(f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}")
        return (res.get("response") or {}).get("player_count")
    except Exception:
        return None


def release_age_days(text):
    if not text:
        return None
    from datetime import datetime
    for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y"):
        try:
            return (T.NOW - datetime.strptime(text.strip(), fmt).timestamp()) / 86400
        except ValueError:
            pass
    return None


# ------------------------------------------------------------------ buzz
def buzz_titles():
    titles = []
    for sub in REDDIT_SUBS:
        try:
            res = get(f"https://www.reddit.com/r/{sub}/hot.json?limit=100", tries=1)
            titles += [c["data"].get("title", "") for c in res.get("data", {}).get("children", [])]
        except Exception as e:
            print(f"reddit r/{sub} skipped:", str(e)[:80])
            break  # if Reddit blocks one, it blocks all
    key = os.environ.get("YOUTUBE_API_KEY")
    if key:
        for region in ("US", "GB"):
            try:
                res = get("https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode({
                    "part": "snippet", "chart": "mostPopular", "videoCategoryId": "20",
                    "regionCode": region, "maxResults": 50, "key": key}))
                for v in res.get("items", []):
                    sn = v.get("snippet", {})
                    titles.append(sn.get("title", "") + " " + " ".join(sn.get("tags", [])[:15]))
            except Exception as e:
                print(f"youtube {region} skipped:", str(e)[:80])
    return [T.norm(t) for t in titles]


def buzz_count(name, titles):
    n = T.norm(name)
    if len(n) < 4:
        return 0
    pat = re.compile(r"\b" + re.escape(n) + r"\b")
    return sum(1 for t in titles if pat.search(t))


# ------------------------------------------------------------------ scoring
def score(idea):
    demand = math.log10(max(idea["players"], 1))
    if idea.get("growth24") is not None:
        demand += max(min(idea["growth24"], 300), -50) / 100
    if idea.get("rankJump"):
        demand += min(idea["rankJump"], 80) / 40
    if idea.get("newRelease"):
        demand += 0.7
    demand += min(idea.get("buzz", 0), 10) * 0.15
    r = idea.get("roblox")
    if not r or r.get("count") is None:
        return round(demand, 2), None
    biggest = max([x["players"] for x in r.get("examples", [])] or [0])
    competition = math.log10(1 + biggest) + min(r["count"], 30) / 15
    return round(demand - competition, 2), biggest


# ------------------------------------------------------------------ main entry
def scout(st, roblox_games, dry):
    ts = st.setdefault("scout", {"lastScan": 0, "history": {}, "details": {}, "roblox": {}, "lastAlert": {}})
    if ts.get("version") == SCOUT_VERSION and T.NOW - ts["lastScan"] < SCOUT_EVERY_MIN * 60 - 30:
        return st.get("scoutOut")
    ts["version"] = SCOUT_VERSION
    ts["lastScan"] = T.NOW

    cands = steam_candidates()
    if not cands:
        print("trend scout: no Steam data this time")
        return st.get("scoutOut")
    titles = buzz_titles()

    ideas = []
    for appid, c in cands.items():
        det = steam_details(appid, ts["details"])
        if det.get("type") not in (None, "game"):
            continue
        name = c.get("name") or det.get("name")
        if not name:
            continue
        age = release_age_days(det.get("release"))
        if age is None or age < -1 or age > STEAM_MAX_AGE_DAYS:
            continue  # only newly released games
        players = steam_players(appid)
        time.sleep(0.15)
        if players is None:
            continue
        hist = ts["history"].setdefault(str(appid), [])
        hist.append([round(T.NOW), players])
        g24 = T.growth(hist, T.NOW - 26 * 3600)
        g24pct = g24["pct"] if g24 and g24["minutes"] >= 6 * 60 else None   # need 6h+ of history
        g3 = T.growth(hist, T.NOW - 3 * 3600 - 300)
        jump = None
        if c.get("chartRank"):
            lw = c.get("lastWeekRank") or 0
            jump = 101 - c["chartRank"] if lw == 0 else lw - c["chartRank"]
        idea = {"appid": appid, "name": name, "players": players, "growth24": g24pct,
                "growth3h": g3["pct"] if g3 and g3["minutes"] >= 60 else None,
                "chartRank": c.get("chartRank"), "rankJump": jump if jump and jump > 0 else None,
                "newRelease": bool(age is not None and age <= NEW_RELEASE_DAYS),
                "release": det.get("release"), "topSeller": bool(c.get("top_sellers")),
                "image": c.get("image") or det.get("image"), "buzz": buzz_count(name, titles),
                "url": f"https://store.steampowered.com/app/{appid}/"}
        why = []
        if g24pct is not None and g24pct >= STEAM_GROWTH_PCT:
            why.append(f"+{g24pct:.0f}% players in 24h")
        if idea["growth3h"] is not None and idea["growth3h"] >= STEAM_GROWTH_PCT / 2:
            why.append(f"+{idea['growth3h']:.0f}% in 3h")
        if idea["rankJump"] and idea["rankJump"] >= STEAM_RANK_JUMP and c.get("chartRank"):
            why.append("new in Steam's most played" if not c.get("lastWeekRank")
                       else f"climbed {idea['rankJump']} places on Steam this week")
        if idea["newRelease"] and idea["topSeller"]:
            why.append(f"new release ({det.get('release')}) in Steam top sellers")
        elif c.get("popular_new") and players >= STEAM_MIN_PLAYERS * 3:
            why.append(f"one of Steam's most popular new releases ({det.get('release')})")
        if idea["buzz"] >= 3:
            why.append(f"mentioned in {idea['buzz']} trending posts/videos")
        idea["why"] = why
        idea["hot"] = players >= STEAM_MIN_PLAYERS and bool(why)
        ideas.append(idea)

    # prune Steam history to 48h
    for k in list(ts["history"]):
        ts["history"][k] = [p for p in ts["history"][k] if p[0] >= T.NOW - 48 * 3600]
        if not ts["history"][k]:
            del ts["history"][k]

    # Roblox competition: hot ideas first, then the biggest, a few per scan (cached 12h)
    order = sorted(ideas, key=lambda i: (not i["hot"], -i["players"]))
    checks = 0
    for idea in order:
        cached = ts["roblox"].get(str(idea["appid"]))
        if cached and T.NOW - cached.get("checkedAt", 0) < 12 * 3600:
            idea["roblox"] = cached
            continue
        if checks >= ROBLOX_CHECKS_PER_SCAN:
            idea["roblox"] = cached
            continue
        checks += 1
        r = T.find_similar({"name": idea["name"], "universeId": -1}, roblox_games)
        if r.get("count") is not None:
            ts["roblox"][str(idea["appid"])] = r
        idea["roblox"] = r if r.get("count") is not None else cached
        time.sleep(2)

    alerts = []
    for idea in ideas:
        idea["score"], idea["robloxBiggest"] = score(idea)
        idea["open"] = idea["robloxBiggest"] is not None and idea["robloxBiggest"] < ROBLOX_OPEN_MAX_PLAYERS
        key = str(idea["appid"])
        if (idea["hot"] and idea["open"]
                and T.NOW - ts["lastAlert"].get(key, 0) > TREND_ALERT_COOLDOWN_DAYS * 86400):
            ts["lastAlert"][key] = T.NOW
            alerts.append(make_alert(idea))

    ideas.sort(key=lambda i: (i["score"] if i["score"] is not None else -99), reverse=True)
    out = {"scannedAt": T.NOW, "ideas": ideas[:60]}
    st["scoutOut"] = out
    print(f"trend scout: {len(ideas)} Steam games, {sum(i['hot'] for i in ideas)} blowing up, "
          f"{sum(1 for i in ideas if i['hot'] and i['open'])} with no big Roblox game, {len(alerts)} alerts")
    for a in alerts:
        print("TREND ALERT:\n" + T.alert_text(a) + "\n")
        if not dry:
            T.send_all(a)
    st["alerts"] = (alerts + st.get("alerts", []))[:200]
    return out


def make_alert(idea):
    r = idea.get("roblox") or {}
    biggest = idea.get("robloxBiggest") or 0
    return {"time": T.NOW, "kind": "trend", "universeId": None, "name": idea["name"],
            "players": idea["players"], "rank": idea.get("chartRank") or "–",
            "reason": "; ".join(idea["why"]), "url": idea["url"], "icon": idea.get("image"),
            "similar": r, "ageText": idea.get("release") or "unknown",
            "listTitle": "Steam", "score": idea["score"],
            "timing": [f"Biggest Roblox look-alike has {biggest:,} playing → wide open"
                       if biggest < 200 else f"Biggest Roblox look-alike has only {biggest:,} playing"]}
