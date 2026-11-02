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

UA = {"User-Agent": "Mozilla/5.0 (personal sports dashboard)"}
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
            out.append({
                "follow_id": follow["id"], "kind": "team", "source_id": e.get("idEvent"),
                "status": "finished" if finished else "upcoming",
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

def youtube_channel(follow, src):
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(get(YOUTUBE.format(src["channel_id"])))
    out = []
    for entry in root.findall("a:entry", ns):
        title = entry.findtext("a:title", namespaces=ns) or ""
        if not mentions(title, follow):
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
            if todo(src.get("channel_id")) or src["type"] not in HIGHLIGHTS:
                continue
            highlights += run(f, src["name"], HIGHLIGHTS[src["type"]], src)

    news.sort(key=lambda n: n["published_at"] or "", reverse=True)
    matches.sort(key=lambda m: m.get("starts_at") or (m.get("dates") or {}).get("start") or "")
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
