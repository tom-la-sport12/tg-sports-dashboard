#!/usr/bin/env python3
"""Pull news, matches and highlights for everything in follows.yaml into data.json.

Each source is wrapped on its own, so one broken feed logs an error
instead of killing the whole run. Needs: pip install pyyaml
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import yaml

UA = {
    "User-Agent": "Mozilla/5.0 (personal sports dashboard)",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cookie": "CONSENT=YES+1; SOCS=CAI",  # skips YouTube's cookie consent page
}
TSDB = "https://www.thesportsdb.com/api/v1/json/123"  # free key
# Unofficial ESPN endpoint: undocumented, can change without warning
ESPN = "https://site.api.espn.com/apis/site/v2/sports/golf/{league}/scoreboard"
ESPN_LEAGUES = {"pga-tour": "pga", "majors": "pga", "dp-world-tour": "eur"}
YOUTUBE = "https://www.youtube.com/feeds/videos.xml?channel_id={}"  # no API key needed

errors = []


# ---------- helpers ----------

def get(url, as_json=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return json.loads(body) if as_json else body


def todo(value):
    return value in (None, "", "TODO")


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def norm(text):
    return re.sub(r"[^\w ]", "", text.lower()).strip()


def has_word(text, phrase):
    return re.search(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)", text.lower()) is not None


def mentions(text, follow):
    """True if text names the follow and doesn't hit an exclude term."""
    if any(has_word(text, x) for x in follow.get("exclude", [])):
        return False
    return any(has_word(text, a) for a in follow.get("aliases", []))


def excluded(text, follow):
    return any(has_word(text, x) for x in follow.get("exclude", []))


def run(follow, label, fn, *args):
    try:
        return fn(follow, *args) or []
    except Exception as e:  # noqa: BLE001
        errors.append({"follow_id": follow["id"], "source": label, "error": f"{type(e).__name__}: {e}"})
        return []


# ---------- news ----------

def parse_rss(xml_bytes, default_source):
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or default_source).strip()
        src_el = item.find("source")
        site = src_el.get("url", "") if src_el is not None else ""
        # Google News appends " - Source" to titles
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        pub = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            published = None
        yield title, (item.findtext("link") or "").strip(), source, published, site


def google_news(follow, src):
    lang = src.get("lang", "en-GB")
    hl, gl = lang.split("-")
    q = urllib.parse.quote(src["query"])
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={gl}&ceid={gl}:{hl}"
    out = []
    for title, link, source, published, site in parse_rss(get(url), "Google News"):
        if excluded(title, follow):
            continue
        if follow.get("title_must_mention") and not mentions(title, follow):
            continue
        out.append({"follow_id": follow["id"], "title": title, "source": source, "site": site,
                    "url": link, "published_at": iso(published), "lang": hl})
    return out


def podcast_rss(follow, src):
    out = []
    for title, link, _, published, _site in parse_rss(get(src["url"]), src["name"]):
        out.append({"follow_id": follow["id"], "title": title, "source": src["name"],
                    "url": link, "published_at": iso(published), "lang": "en", "kind": "podcast"})
    return out[:5]


# ---------- matches ----------

_last_tsdb = [0.0]
_badges = {}
images = {}  # follow_id -> avatar image, written into data.json


def tsdb(path):
    """TheSportsDB call: spaced out, and backs off if the shared free key is busy."""
    for attempt in range(4):
        wait = 3.0 - (time.time() - _last_tsdb[0])
        if wait > 0:
            time.sleep(wait)
        _last_tsdb[0] = time.time()
        try:
            return get(f"{TSDB}/{path}", as_json=True) or {}
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise
            time.sleep(15 * (attempt + 1))  # 15s, 30s, 45s
    return {}


def badge(team_id, hint=None):
    if hint:
        return hint
    if not team_id:
        return None
    team_id = str(team_id)
    if team_id not in _badges:
        try:
            teams = tsdb(f"lookupteam.php?id={team_id}").get("teams") or [{}]
            _badges[team_id] = teams[0].get("strBadge") or teams[0].get("strTeamBadge")
        except Exception:  # noqa: BLE001
            _badges[team_id] = None
    return _badges[team_id]


def thesportsdb(follow, fx):
    now = datetime.now(timezone.utc)
    me = str(fx["team_id"])
    images[follow["id"]] = badge(me)
    out = []
    for endpoint in ("eventsnext", "eventslast"):
        data = tsdb(f"{endpoint}.php?id={me}")
        for e in data.get("events") or data.get("results") or []:
            home, away = e.get("intHomeScore"), e.get("intAwayScore")
            finished = home not in (None, "") and away not in (None, "")
            ts = e.get("strTimestamp")
            if not ts and e.get("dateEvent"):
                ts = f"{e['dateEvent']}T{e.get('strTime') or '00:00:00'}"
            starts = None
            if ts:
                starts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if starts.tzinfo is None:
                    starts = starts.replace(tzinfo=timezone.utc)  # TheSportsDB timestamps are UTC
            # cricket results come as text, not scores, so also go by date
            if finished or (starts and starts < now):
                status = "finished"
            else:
                status = "upcoming"
            out.append({
                "follow_id": follow["id"], "kind": "team", "source_id": e.get("idEvent"),
                "status": status, "result": e.get("strResult") or None,
                "starts_at": iso(starts), "competition": e.get("strLeague"),
                "home": e.get("strHomeTeam"), "away": e.get("strAwayTeam"),
                "score": {"home": int(home), "away": int(away)} if (home not in (None, "") and away not in (None, "")) else None,
                "venue": e.get("strVenue"), "city": e.get("strCity"), "country": e.get("strCountry"),
                "round": e.get("intRound"), "season": e.get("strSeason"),
                "side": "home" if str(e.get("idHomeTeam")) == me else "away",
                "home_id": e.get("idHomeTeam"), "away_id": e.get("idAwayTeam"),
                "home_badge": badge(e.get("idHomeTeam"), e.get("strHomeTeamBadge")),
                "away_badge": badge(e.get("idAwayTeam"), e.get("strAwayTeamBadge")),
                "thumb": e.get("strThumb") or None,
                "video": e.get("strVideo") or None,
                "description": e.get("strDescriptionEN") or None,
                "spectators": e.get("intSpectators") or None,
            })
    return out


PREV = {}  # last run's data.json, set in main()


def golfer_row(c):
    score = c.get("score")
    if isinstance(score, dict):
        score = score.get("displayValue")
    ath = c.get("athlete") or {}
    return {
        "id": str(ath.get("id") or c.get("id")),
        "name": ath.get("displayName") or ath.get("shortName") or "",
        "pos": (((c.get("status") or {}).get("position")) or {}).get("displayName"),
        "to_par": score,
    }


def previous_positions(event_id):
    """Positions from last run for the same event, to show who's moved."""
    out = {}
    for m in PREV.get("matches", []):
        if m.get("kind") == "golf" and str(m.get("source_id")) == str(event_id):
            for r in m.get("leaderboard") or []:
                out[r["id"]] = r.get("pos")
            out[str(m.get("player_id"))] = m.get("position")
    return out


def espn_golf(follow, ev):
    """Find this player in whichever event is on this week. Empty list = not playing."""
    pid = str(ev["player_id"])
    images[follow["id"]] = f"https://a.espncdn.com/i/headshots/golf/players/full/{pid}.png"
    leagues = sorted({ESPN_LEAGUES[t] for t in follow.get("tours", []) if t in ESPN_LEAGUES})
    out, seen = [], set()
    for league in leagues:
        data = get(ESPN.format(league=league), as_json=True)
        for event in data.get("events", []):
            if event.get("id") in seen:
                continue  # co-sanctioned events appear on both boards
            for comp in event.get("competitions", []):
                for c in comp.get("competitors", []):
                    if pid not in (str(c.get("id")), str((c.get("athlete") or {}).get("id"))):
                        continue
                    seen.add(event.get("id"))
                    state = ((event.get("status") or {}).get("type") or {}).get("state")
                    score = c.get("score")
                    if isinstance(score, dict):
                        score = score.get("displayValue")
                    position = (((c.get("status") or {}).get("position")) or {}).get("displayName")
                    ranked = sorted(comp.get("competitors", []),
                                    key=lambda x: int(x.get("order") or x.get("sortOrder") or 9999))
                    before = previous_positions(event.get("id"))
                    board = [golfer_row(x) for x in ranked[:5]]
                    for r in board:
                        r["prev_pos"] = before.get(r["id"])
                    out.append({
                        "player_id": pid, "prev_position": before.get(pid), "leaderboard": board,
                        "field_size": len(ranked),
                        "follow_id": follow["id"], "kind": "golf", "source_id": event.get("id"),
                        "status": {"pre": "upcoming", "in": "in_progress", "post": "finished"}.get(state, state),
                        "event": event.get("name"), "tour": league,
                        "dates": {"start": event.get("date"), "end": event.get("endDate")},
                        "position": position, "to_par": score,
                        "round": (event.get("status") or {}).get("period"),
                    })
    return out


# ---------- highlights ----------

_channel_ids = {}
_channel_images = {}


def channel_id(src):
    """Use channel_id if given, otherwise look it up from the @handle."""
    if not todo(src.get("channel_id")):
        return src["channel_id"]
    handle = src["handle"].strip()
    direct = re.search(r"/channel/(UC[\w-]{22})", handle)
    if direct:  # a pasted /channel/UC... link already contains the ID
        return direct.group(1)
    if handle.startswith("http"):
        url = handle  # any pasted channel link
    else:
        url = "https://www.youtube.com/" + (handle if handle.startswith("@") else "@" + handle)
    if handle not in _channel_ids:
        html = get(url).decode("utf-8", "ignore")
        og = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        _channel_images[handle] = og.group(1) if og else None
        m = (re.search(r'"externalId":"(UC[\w-]{22})"', html)
             or re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
             or re.search(r'"channelId":"(UC[\w-]{22})"', html))
        if not m:
            raise ValueError(f"couldn't find a channel ID for {handle}; check the handle is right")
        _channel_ids[handle] = m.group(1)
    return _channel_ids[handle]


def link_href(entry, ns):
    link = entry.find("a:link", ns)
    return link.get("href", "") if link is not None else ""


def youtube_channel(follow, src):
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(get(YOUTUBE.format(channel_id(src))))
    if follow.get("kind") == "creator" and follow["id"] not in images:
        h = (src.get("handle") or "").strip()
        images[follow["id"]] = _channel_images.get(h) or _channel_images.get("@" + h.lstrip("@"))
    keep_all = src.get("match_on") == "all"
    out = []
    for entry in root.findall("a:entry", ns):
        title = entry.findtext("a:title", namespaces=ns) or ""
        if keep_all:
            if excluded(title, follow):
                continue
        elif not mentions(title, follow):
            continue
        pub = entry.findtext("a:published", namespaces=ns)
        out.append({
            "follow_id": follow["id"], "title": title, "channel": src["name"],
            "video_id": entry.findtext("yt:videoId", namespaces=ns),
            "short": "/shorts/" in link_href(entry, ns),
            "published_at": iso(datetime.fromisoformat(pub)) if pub else None,
            "linked_match": None,  # v2: match on team names + date window
        })
    return out


# ---------- news images ----------
# Google News links are wrapped, and the feed carries no pictures. To get one we
# (1) ask Google for the publisher link behind the wrapper, then (2) read that
# article's og:image. This uses Google's unofficial internal endpoint, so it may
# break one day; if it does, the page falls back to the outlet's logo.

GN_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
IMAGE_LOOKUPS_PER_RUN = 40


def gn_publisher_url(google_url):
    m = re.search(r"/articles/([^/?]+)", google_url)
    if not m:
        return None
    art = m.group(1)
    html = get(f"https://news.google.com/rss/articles/{art}?hl=en-GB&gl=GB&ceid=GB:en").decode("utf-8", "ignore")
    sg = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    if not (sg and ts):
        return None
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]') % (art, ts.group(1), sg.group(1))
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
    req = urllib.request.Request(GN_BATCH, data=body.encode(), headers={
        **UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode("utf-8", "ignore")
    found = re.search(r'garturlres\\",\\"(https?://[^"\\]+)', text)
    if found:
        return found.group(1)
    parts = text.split("\n\n")
    if len(parts) > 1:
        data = json.loads(parts[1])
        return json.loads(data[0][2])[1]
    return None


def og_image(page_url):
    req = urllib.request.Request(page_url, headers=UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        html = r.read(400_000).decode("utf-8", "ignore")
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, html, re.I)
        if m:
            img = m.group(1).replace("&amp;", "&")
            return urllib.parse.urljoin(page_url, img)
    return None


def add_news_images(news, prev):
    """Reuse images found on earlier runs; look up a capped number of new ones."""
    known = {n["url"]: n for n in prev.get("news", []) if n.get("img_tried")}
    budget = IMAGE_LOOKUPS_PER_RUN
    for n in news:
        old = known.get(n["url"])
        if old:
            n["article_url"] = old.get("article_url")
            n["image"] = old.get("image")
            n["img_tried"] = True
            continue
        if "news.google.com" not in n["url"] or budget <= 0:
            continue
        budget -= 1
        n["img_tried"] = True  # don't keep retrying the same article every run
        try:
            time.sleep(1)
            real = gn_publisher_url(n["url"])
            n["article_url"] = real
            n["image"] = og_image(real) if real else None
        except Exception:  # noqa: BLE001  any failure just means no picture
            n.setdefault("image", None)


# ---------- main ----------

NEWS = {"google_news": google_news, "podcast_rss": podcast_rss}
FIXTURES = {"thesportsdb": thesportsdb}
EVENTS = {"espn_golf": espn_golf}
HIGHLIGHTS = {"youtube_channel": youtube_channel}


# ---------- news quality ----------

NEWS_DEFAULTS = {
    "max_per_follow": 10,
    "max_age_days": 7,
    "skip_phrases": [],
    "blocked_sources": [],
}
STOP = {"with", "from", "that", "this", "after", "their", "have", "will", "into", "over",
        "what", "when", "they", "about", "says", "more", "than", "just", "were", "been"}


def words(title):
    return {w for w in re.findall(r"[\w']+", title.lower()) if len(w) >= 4 and w not in STOP}


def same_story(a, b):
    """Two headlines about the same thing: most of the shorter one's key words appear in the other."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    overlap = len(wa & wb) / min(len(wa), len(wb))
    return overlap >= 0.6


def quality_filter(items, follow, settings, filtered):
    """Drop fluff, blocked outlets, old stories and repeats; keep the newest few."""
    phrases = [p.lower() for p in settings["skip_phrases"] + follow.get("skip_phrases", [])]
    blocked = {b.lower() for b in settings["blocked_sources"] + follow.get("blocked_sources", [])}
    cutoff = datetime.now(timezone.utc).timestamp() - settings["max_age_days"] * 86400
    cap = follow.get("max_stories", settings["max_per_follow"])
    items = sorted(items, key=lambda n: n["published_at"] or "", reverse=True)
    kept = []
    for n in items:
        title = n["title"].lower()
        if any(p in title for p in phrases):
            filtered["phrase"] += 1
            continue
        if n.get("source", "").lower() in blocked:
            filtered["source"] += 1
            continue
        if n.get("kind") != "podcast" and n["published_at"] and datetime.fromisoformat(n["published_at"]).timestamp() < cutoff:
            filtered["old"] += 1
            continue
        if any(norm(n["title"]) == norm(k["title"]) or same_story(n["title"], k["title"]) for k in kept):
            filtered["duplicate"] += 1
            continue
        if len(kept) >= cap:
            filtered["cap"] += 1
            continue
        kept.append(n)
    return kept


def load_previous():
    """Last run's data.json: reused for badges, and as a fallback if a source fails."""
    try:
        prev = json.load(open("data.json", encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for m in prev.get("matches", []):
        for side in ("home", "away"):
            if m.get(f"{side}_id") and m.get(f"{side}_badge"):
                _badges[str(m[f"{side}_id"])] = m[f"{side}_badge"]
    for f in prev.get("follows", []):
        if f.get("image"):
            images[f["id"]] = f["image"]
    return prev


def main():
    config = yaml.safe_load(open("follows.yaml", encoding="utf-8"))
    follows = config["follows"]
    settings = {**NEWS_DEFAULTS, **(config.get("news_settings") or {})}
    filtered = {"phrase": 0, "source": 0, "old": 0, "duplicate": 0, "cap": 0}
    prev = load_previous()
    PREV.update(prev)
    news, matches, highlights = [], [], []

    for f in follows:
        f_news = []
        for src in f.get("news", []):
            if todo(src.get("url", "ok")) or src["type"] not in NEWS:
                continue
            f_news += run(f, src["type"], NEWS[src["type"]], src)
        news += quality_filter(f_news, f, settings, filtered)

        fx = f.get("fixtures")
        if fx and not todo(fx.get("team_id")) and fx["provider"] in FIXTURES:
            failed_before = len(errors)
            got = run(f, fx["provider"], FIXTURES[fx["provider"]], fx)
            if len(errors) > failed_before and not got:
                # source failed: keep last run's fixtures rather than showing nothing
                got = [m for m in prev.get("matches", []) if m.get("follow_id") == f["id"] and m.get("kind") == "team"]
                errors[-1]["error"] += " (showing last run's fixtures)"
            matches += got

        ev = f.get("events")
        if ev and not todo(ev.get("player_id")) and ev["provider"] in EVENTS:
            matches += run(f, ev["provider"], EVENTS[ev["provider"]], ev)

        for src in f.get("highlights", []):
            if todo(src.get("channel_id")) and todo(src.get("handle")):
                continue
            if src["type"] not in HIGHLIGHTS:
                continue
            highlights += run(f, src["name"], HIGHLIGHTS[src["type"]], src)

    news.sort(key=lambda n: n["published_at"] or "", reverse=True)
    matches = list({(m["follow_id"], m["source_id"]): m for m in matches}.values())
    matches.sort(key=lambda m: m.get("starts_at") or (m.get("dates") or {}).get("start") or "")
    add_news_images(news, prev)
    # same video can come from two channels' rules (e.g. PGA TOUR for two golfers): keep one per follow
    highlights = list({(h["follow_id"], h["video_id"]): h for h in highlights}.values())
    highlights.sort(key=lambda h: h["published_at"] or "", reverse=True)

    data = {
        "generated_at": iso(datetime.now(timezone.utc)),
        "follows": [{"id": f["id"], "name": f["name"], "sport": f["sport"], "kind": f["kind"],
                     "image": images.get(f["id"])} for f in follows],
        "news": news, "matches": matches, "highlights": highlights, "errors": errors,
    }
    with open("data.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("news filtered out: " + ", ".join(f"{v} {k}" for k, v in filtered.items()))
    print(f"news {len(news)} · matches {len(matches)} · highlights {len(highlights)} · errors {len(errors)}")
    for e in errors:
        print(f"  ! {e['follow_id']} / {e['source']}: {e['error']}")


if __name__ == "__main__":
    main()
