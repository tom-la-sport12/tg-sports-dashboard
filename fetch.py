#!/usr/bin/env python3
"""Pull news, matches and highlights for everything in follows.yaml into data.json.

Each source is wrapped on its own, so one broken feed logs an error
instead of killing the whole run. Needs: pip install pyyaml
"""
import json
import re
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
NEWS_PER_FOLLOW = 15

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
        # Google News appends " - Source" to titles
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        pub = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            published = None
        yield title, (item.findtext("link") or "").strip(), source, published


def google_news(follow, src):
    lang = src.get("lang", "en-GB")
    hl, gl = lang.split("-")
    q = urllib.parse.quote(src["query"])
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={gl}&ceid={gl}:{hl}"
    out = []
    for title, link, source, published in parse_rss(get(url), "Google News"):
        if excluded(title, follow):
            continue
        if follow.get("title_must_mention") and not mentions(title, follow):
            continue
        out.append({"follow_id": follow["id"], "title": title, "source": source,
                    "url": link, "published_at": iso(published), "lang": hl})
    return out


def podcast_rss(follow, src):
    out = []
    for title, link, _, published in parse_rss(get(src["url"]), src["name"]):
        out.append({"follow_id": follow["id"], "title": title, "source": src["name"],
                    "url": link, "published_at": iso(published), "lang": "en", "kind": "podcast"})
    return out[:5]


# ---------- matches ----------

def thesportsdb(follow, fx):
    now = datetime.now(timezone.utc)
    out = []
    for endpoint in ("eventsnext", "eventslast"):
        data = get(f"{TSDB}/{endpoint}.php?id={fx['team_id']}", as_json=True) or {}
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
                "score": {"home": int(home), "away": int(away)} if finished else None,
                "venue": e.get("strVenue"),
            })
    return out


def espn_golf(follow, ev):
    """Find this player in whichever event is on this week. Empty list = not playing."""
    pid = str(ev["player_id"])
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
                    out.append({
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
        m = (re.search(r'"externalId":"(UC[\w-]{22})"', html)
             or re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
             or re.search(r'"channelId":"(UC[\w-]{22})"', html))
        if not m:
            raise ValueError(f"couldn't find a channel ID for {handle}; check the handle is right")
        _channel_ids[handle] = m.group(1)
    return _channel_ids[handle]


def youtube_channel(follow, src):
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(get(YOUTUBE.format(channel_id(src))))
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
            "published_at": iso(datetime.fromisoformat(pub)) if pub else None,
            "linked_match": None,  # v2: match on team names + date window
        })
    return out


# ---------- main ----------

NEWS = {"google_news": google_news, "podcast_rss": podcast_rss}
FIXTURES = {"thesportsdb": thesportsdb}
EVENTS = {"espn_golf": espn_golf}
HIGHLIGHTS = {"youtube_channel": youtube_channel}


def main():
    follows = yaml.safe_load(open("follows.yaml", encoding="utf-8"))["follows"]
    news, matches, highlights = [], [], []

    for f in follows:
        f_news = []
        for src in f.get("news", []):
            if todo(src.get("url", "ok")) or src["type"] not in NEWS:
                continue
            f_news += run(f, src["type"], NEWS[src["type"]], src)
        # newest first, drop duplicate headlines across outlets
        f_news.sort(key=lambda n: n["published_at"] or "", reverse=True)
        seen = set()
        for n in f_news:
            key = norm(n["title"])
            if key not in seen:
                seen.add(key)
                news.append(n)
                if len(seen) >= NEWS_PER_FOLLOW:
                    break

        fx = f.get("fixtures")
        if fx and not todo(fx.get("team_id")) and fx["provider"] in FIXTURES:
            matches += run(f, fx["provider"], FIXTURES[fx["provider"]], fx)

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
    # same video can come from two channels' rules (e.g. PGA TOUR for two golfers): keep one per follow
    highlights = list({(h["follow_id"], h["video_id"]): h for h in highlights}.values())
    highlights.sort(key=lambda h: h["published_at"] or "", reverse=True)

    data = {
        "generated_at": iso(datetime.now(timezone.utc)),
        "follows": [{"id": f["id"], "name": f["name"], "sport": f["sport"], "kind": f["kind"]} for f in follows],
        "news": news, "matches": matches, "highlights": highlights, "errors": errors,
    }
    with open("data.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print(f"news {len(news)} · matches {len(matches)} · highlights {len(highlights)} · errors {len(errors)}")
    for e in errors:
        print(f"  ! {e['follow_id']} / {e['source']}: {e['error']}")


if __name__ == "__main__":
    main()
