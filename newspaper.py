"""Mechanical steps of the /feed run. The judging is done by your agent CLI; see AGENTS.md.

Everything the reader owns lives in data/ (or $NEWSPAPER_DATA): profile, feeds, history, votes.

  python newspaper.py init [--opml FILE]  create data/ for a new reader
  python newspaper.py sections            the page's sections from data/sections.md, as JSON
  python newspaper.py fetch               new unseen items -> data/state/new_items.json
  python newspaper.py batches triage      split new items into triage batches
  python newspaper.py batches read        split items kept by triage into deep-read batches
  python newspaper.py render              data/state/digest.json + template.html -> data/digest.html
  python newspaper.py commit              mark this run's items as seen, log kept items, clear work files
  python newspaper.py votes [--mark]      votes from the page not learned from yet (--mark: learned)
  python newspaper.py apply-decisions     apply Add/Skip choices from the page's Suggested feeds tab
  python newspaper.py discover-due        "yes" if feed discovery hasn't run today
  python newspaper.py signals             source domains of kept items, for feed discovery
  python newspaper.py probe URL...        check sites or feeds: feed URL, posting rate, recent posts
  python newspaper.py suggest FILE [--add]  merge scout picks into suggested feeds (--add: follow them now)
  python newspaper.py serve [--port N]    serve the page and store its votes, saves and decisions
  python newspaper.py open                start the server in the background if needed, open the page
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urljoin, urlparse
from xml.sax.saxutils import quoteattr

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("NEWSPAPER_DATA") or ROOT / "data").resolve()
STATE = DATA / "state"
WORK = STATE / "work"
SEEN = STATE / "seen.json"
NEW = STATE / "new_items.json"
DIGEST = STATE / "digest.json"
KEPT = STATE / "kept.jsonl"
SUGGESTIONS = STATE / "feed_suggestions.json"
DISCOVERY = STATE / "discovery.json"
VOTES_APPLIED = STATE / "votes_applied.json"
OPML = DATA / "feeds.opml"
PROFILE = DATA / "profile.md"
SECTIONS = DATA / "sections.md"
PAGE = DATA / "digest.html"
COLLECTIONS = ("votes", "saved", "feed_decisions")  # written by the page through the server
PORT = 8765
VERSION = str(Path(__file__).stat().st_mtime_ns)  # a running server older than this code gets restarted
HOST = "newspaper.localhost"  # browsers send any *.localhost name to this machine, no hosts file needed
UA = "Mozilla/5.0 (compatible; newspaper/1.0; personal RSS reader)"
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
FIRST_RUN_DAYS = 7
MAX_AGE_DAYS = 30
SEEN_KEEP_DAYS = 120
EMPTY_OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head>
    <title>newspaper feeds</title>
  </head>
  <body>
  </body>
</opml>
"""


def now():
    return datetime.now(timezone.utc)


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def show_path(path):
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path.as_posix()


def clean(text, limit=700):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:limit]


def parse_date(text):
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.strip())
        except ValueError:
            return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def item_id(link, title):
    return hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:16]


def parse_feed(source, data):
    root = ET.fromstring(data)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        title = clean(it.findtext("title"), 300)
        items.append({
            "title": title, "link": link,
            "comments": (it.findtext("comments") or "").strip(),
            "published": parse_date(it.findtext("pubDate")),
            "summary": clean(it.findtext("description")),
        })
    for e in root.iter(f"{ATOM}entry"):
        links = e.findall(f"{ATOM}link")
        alt = next((l for l in links if l.get("rel", "alternate") == "alternate"), links[0] if links else None)
        summary = e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content") or ""
        if not summary:
            summary = e.findtext(f"{MEDIA}group/{MEDIA}description") or ""
        items.append({
            "title": clean(e.findtext(f"{ATOM}title"), 300),
            "link": alt.get("href", "").strip() if alt is not None else "",
            "comments": "",
            "published": parse_date(e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated")),
            "summary": clean(summary),
        })
    for i in items:
        i["id"] = item_id(i["link"], i["title"])
        i["source"] = source
        i["kind"] = "video" if "youtube.com" in i["link"] or "youtu.be" in i["link"] else "text"
    return items


def get(url, headers=None):
    with urlopen(Request(url, headers={"User-Agent": UA, **(headers or {})}), timeout=25) as r:
        return r.read(), r.geturl()


# YouTube's RSS endpoint fails most requests (404/500), so a channel's videos page is the fallback.
YT_CHANNEL = re.compile(r"channel_id=(UC[\w-]{22})")
AGO = {"min": "minutes", "h": "hours", "d": "days", "w": "weeks", "mo": "months", "y": "years"}


def ago_to_date(text):
    m = re.match(r"(\d+)\s*(mo|min|[hdwy])", (text or "").replace("Streamed ", ""))
    if not m:
        return None
    n, unit = int(m.group(1)), AGO[m.group(2)]
    days = {"minutes": n / 1440, "hours": n / 24, "days": n, "weeks": 7 * n, "months": 30 * n, "years": 365 * n}[unit]
    return now() - timedelta(days=days)


def youtube_channel_page(channel_id):
    data, _ = get(f"https://www.youtube.com/channel/{channel_id}/videos", {"Accept-Language": "en-US,en"})
    page = data.decode("utf-8", "replace")
    return json.loads(re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S).group(1))


def youtube_items(source, channel_id):
    found = []

    def walk(o):
        if isinstance(o, dict):
            if "lockupViewModel" in o:
                found.append(o["lockupViewModel"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(youtube_channel_page(channel_id))
    items = []
    for v in found:
        meta = v.get("metadata", {}).get("lockupMetadataViewModel", {})
        rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
        parts = [p.get("text", {}).get("content", "") for r in rows for p in r.get("metadataParts", [])]
        link = f"https://www.youtube.com/watch?v={v.get('contentId')}"
        items.append({"title": clean(meta.get("title", {}).get("content"), 300), "link": link, "comments": "",
                      "published": next((d for d in map(ago_to_date, parts) if d), None), "summary": "",
                      "id": item_id(link, ""), "source": source, "kind": "video"})
    if not items:
        raise ValueError("no videos on channel page")
    return items


def feed_title(data):
    root = ET.fromstring(data)
    if root.tag.split("}")[-1] not in ("rss", "feed", "RDF"):
        raise ET.ParseError(f"not a feed: <{root.tag}>")
    return clean(root.findtext("channel/title") or root.findtext(f"{ATOM}title"), 200)


def outlines():
    return [o for o in ET.parse(OPML).getroot().iter("outline") if o.get("xmlUrl")]


def add_outline(name, feed_url, site):
    line = (f'    <outline text={quoteattr(name)} title={quoteattr(name)} type="rss" '
            f'xmlUrl={quoteattr(feed_url)} htmlUrl={quoteattr(site or "")}/>\n')
    text = OPML.read_text(encoding="utf-8")
    OPML.write_text(re.sub(r"([ \t]*)</body>", lambda m: line + m.group(0), text, count=1), encoding="utf-8")


def fetch_one(outline):
    source, url = outline.get("title") or outline.get("text"), outline.get("xmlUrl")
    yt = YT_CHANNEL.search(url or "")
    err = None
    for attempt in range(3 if yt else 1):
        try:
            return source, parse_feed(source, get(url)[0]), None
        except Exception as e:  # one broken feed must not stop the run
            err = f"{type(e).__name__}: {e}"
            time.sleep(1)
    for attempt in range(3 if yt else 0):
        try:
            return source, youtube_items(source, yt.group(1)), None
        except Exception as e:
            page_err = f"{type(e).__name__}: {e}"
            time.sleep(1)
    if yt:
        err += f"; channel page: {page_err}"
    return source, [], err


def cmd_init(args):
    if PROFILE.exists():
        print(f"{show_path(DATA)} already set up")
        return
    DATA.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "examples" / "profile.md", PROFILE)
    if not SECTIONS.exists():
        shutil.copy(ROOT / "examples" / "sections.md", SECTIONS)
    if args.opml:
        ET.parse(args.opml)  # fail early on a broken file
        shutil.copy(args.opml, OPML)
    elif not OPML.exists():
        OPML.write_text(EMPTY_OPML, encoding="utf-8")
    print(f"created {show_path(DATA)}: profile.md, feeds.opml ({len(outlines())} feeds)")


def cmd_fetch(_):
    feeds = outlines()
    seen = load(SEEN, None)
    cutoff = now() - timedelta(days=FIRST_RUN_DAYS if seen is None else MAX_AGE_DAYS)
    seen = seen or {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_one, feeds))
    items, errors, per_source = [], {}, {}
    taken = set()
    for source, feed_items, err in results:
        if err:
            errors[source] = err
        for i in feed_items:
            if i["id"] in seen or i["id"] in taken or (i["published"] and i["published"] < cutoff):
                continue
            taken.add(i["id"])
            i["published"] = i["published"].isoformat() if i["published"] else None
            items.append(i)
            per_source[source] = per_source.get(source, 0) + 1
    if feeds and len(errors) == len(feeds):
        for source, err in errors.items():
            print(f"  FAILED {source}: {err}")
        sys.exit("NO NETWORK: every feed failed, so this is a network problem, not the feeds. "
                 "If your CLI runs commands in a sandbox, rerun this command with network access.")
    items.sort(key=lambda i: i["published"] or "", reverse=True)
    save(NEW, {"fetched_at": now().isoformat(), "feeds": len(feeds), "items": items, "errors": errors})
    print(f"{len(items)} new items from {len(feeds)} feeds")
    for source, n in sorted(per_source.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {source}")
    for source, err in errors.items():
        print(f"  FAILED {source}: {err}")


def chunks(seq, size):
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def read_outputs(prefix):
    out = []
    for p in sorted(WORK.glob(f"{prefix}_out_*.json")):
        out.extend(load(p, []))
    return out


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def load_sections():
    """Sections from data/sections.md (the default layout if there is none): one `## Title` per section,
    a description paragraph and `- key: value` settings."""
    path = SECTIONS if SECTIONS.exists() else ROOT / "examples" / "sections.md"
    sections, cur = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            key = slug(title)
            while any(x["key"] == key for x in sections):
                key += "-2"
            cur = {"key": key, "title": title, "desc": "", "order": "", "checks": "strict", "surprise": False, "max": None}
            sections.append(cur)
        elif cur is None or not line.strip():
            continue
        elif m := re.match(r"\s*[-*]\s*(\w+)\s*:\s*(.+)", line):
            k, v = m.group(1).lower(), m.group(2).strip().strip("`")
            if k == "surprise":
                cur[k] = v.lower() in ("yes", "true", "on")
            elif k == "max":
                cur[k] = int(v) if v.isdigit() else None
            elif k == "checks":
                cur[k] = v.lower() if v.lower() in ("strict", "light", "off") else "strict"
            else:
                cur[k] = v
        else:
            cur["desc"] = (cur["desc"] + " " + line.strip()).strip()
    if not sections:
        sys.exit(f"{show_path(path)} has no sections: add at least one `## Title` heading")
    return sections


def cmd_sections(_):
    print(json.dumps(load_sections(), ensure_ascii=False, indent=1))


def cmd_batches(args):
    items = load(NEW, {"items": []})["items"]
    sections = load_sections()
    save(WORK / "sections.json", sections)  # the layout every subtask sorts items into
    if args.stage == "triage":
        fields = ("id", "source", "kind", "title", "summary")
        batch = [{k: i[k] for k in fields} for i in items]
        size = args.size or 60
    else:
        by_key = {x["key"]: x for x in sections}
        surprise_key = next((x["key"] for x in sections if x["surprise"]), None)
        batch = []
        for t in read_outputs("triage"):
            item = next((i for i in items if i["id"] == t["id"]), None)
            if not item:
                continue
            if t.get("keep") and t.get("section") in by_key and not by_key[t["section"]]["surprise"]:
                key = t["section"]
            elif t.get("surprise") and surprise_key and not t.get("keep"):
                key = surprise_key
            else:
                continue
            batch.append(item | {"section": key, "checks": by_key[key]["checks"], "format": t.get("format"),
                                 **({"surprise": True} if key == surprise_key else {})})
        size = args.size or 6
    for old in WORK.glob(f"{args.stage}_in_*.json"):
        old.unlink()
    for n, part in enumerate(chunks(batch, size), 1):
        path = WORK / f"{args.stage}_in_{n}.json"
        save(path, part)
        print(show_path(path))
    print(f"{len(batch)} items in {len(chunks(batch, size))} batches", file=sys.stderr)


def cmd_render(_):
    new = load(NEW, {"items": [], "feeds": 0})
    by_id = {i["id"]: i for i in new["items"]}
    digest = load(DIGEST, {"intro": "", "items": []})
    sections = load_sections()
    by_key = {x["key"]: x for x in sections}
    kept, counts, cut = [], {}, {}
    for d in digest["items"]:
        sec = by_key.get(d.get("section"))
        if not sec:
            print(f"  not on the page: unknown section {d.get('section')!r} for {d.get('title')!r}", file=sys.stderr)
            cut[d["id"]] = ("no-section", f"section {d.get('section')!r} is not in sections.md")
            continue
        counts[sec["key"]] = counts.get(sec["key"], 0) + 1
        if sec["max"] and counts[sec["key"]] > sec["max"]:
            cut[d["id"]] = ("cut by editor", f"over the {sec['title']} limit of {sec['max']}")
            continue
        base = by_id.get(d["id"], {})
        kept.append({**{k: base.get(k) for k in ("source", "link", "comments", "published", "kind")}, **d})
    kept_ids = {k["id"] for k in kept}
    reasons = {}
    for r in read_outputs("triage") + read_outputs("read"):
        if not r.get("keep"):
            reasons[r["id"]] = (r.get("reason") or r.get("drop_reason") or "other", r.get("note", ""))
        elif r.get("section") not in by_key:
            reasons[r["id"]] = ("no-section", f"section {r.get('section')!r} is not in sections.md")
    reasons.update(cut)
    dropped = []
    for i in new["items"]:
        if i["id"] in kept_ids:
            continue
        reason, note = reasons.get(i["id"], ("cut by editor", ""))
        dropped.append({"title": i["title"], "source": i["source"], "link": i["link"], "reason": reason, "note": note})
    page = {
        "generated_at": now().isoformat(), "feeds": new.get("feeds", 0),
        "scanned": len(new["items"]), "intro": digest.get("intro", ""),
        "sections": sections, "items": kept, "dropped": dropped,
        "suggestions": [s for s in load(SUGGESTIONS, []) if s["status"] == "pending"],
    }
    blob = json.dumps(page, ensure_ascii=False).replace("</", "<\\/")
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    PAGE.write_text(template.replace("__DIGEST_JSON__", blob), encoding="utf-8")
    print(f"{show_path(PAGE)}: {len(kept)} kept, {len(dropped)} dropped, {len(new['items'])} scanned")


def cmd_commit(_):
    seen = load(SEEN, {})
    stamp = now().date().isoformat()
    for i in load(NEW, {"items": []})["items"]:
        seen.setdefault(i["id"], stamp)
    horizon = (now() - timedelta(days=SEEN_KEEP_DAYS)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= horizon}
    save(SEEN, seen)
    new = {i["id"]: i for i in load(NEW, {"items": []})["items"]}
    kept = load(DIGEST, {"items": []})["items"]
    with KEPT.open("a", encoding="utf-8") as f:
        for d in kept:
            base = new.get(d["id"], {})
            row = {"date": stamp, "id": d["id"], "title": d.get("title"), "section": d.get("section"),
                   "source": base.get("source", d.get("source")), "link": base.get("link", d.get("link"))}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    shutil.rmtree(WORK, ignore_errors=True)
    for p in (NEW, DIGEST):
        p.unlink(missing_ok=True)
    print(f"marked as seen; {len(seen)} ids tracked")


def cmd_votes(args):
    votes = load(STATE / "votes.json", {})
    applied = load(VOTES_APPLIED, [])
    done = set(applied)
    new = {k: v for k, v in votes.items() if k not in done}
    if args.mark:
        save(VOTES_APPLIED, applied + list(new))
        print(f"{len(new)} votes marked as learned")
    else:
        print(json.dumps(new, ensure_ascii=False, indent=1))


def decide(sugg, sid, decision):
    s = next((s for s in sugg if s["id"] == sid), None)
    if not s or s["status"] != "pending":
        return None
    if decision == "add":
        add_outline(s["name"], s["feed_url"], s["site"])
    s["status"] = "added" if decision == "add" else "skipped"
    s["decided_at"] = now().date().isoformat()
    return s


def cmd_apply_decisions(_):
    sugg = load(SUGGESTIONS, [])
    for sid, d in load(STATE / "feed_decisions.json", {}).items():
        s = decide(sugg, sid, d.get("decision"))
        if s:
            print(f"{s['name']}: {s['status']}")
    save(SUGGESTIONS, sugg)


def domain(url):
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def cmd_signals(_):
    """Sites your feeds keep linking out to: candidates to follow directly."""
    feeds = outlines()
    followed = {domain(o.get("htmlUrl")) for o in feeds} | {domain(o.get("xmlUrl")) for o in feeds}
    home = {o.get("title") or o.get("text"): domain(o.get("htmlUrl")) for o in feeds}
    counts = {}
    for line in KEPT.read_text(encoding="utf-8").splitlines() if KEPT.exists() else []:
        row = json.loads(line)
        d = domain(row.get("link"))
        if d and d not in followed and d != home.get(row.get("source")) and "youtube.com" not in d:
            counts.setdefault(d, []).append(row["title"])
    for d, titles in sorted(counts.items(), key=lambda kv: -len(kv[1])):
        print(f"{len(titles):3d}  {d}  | " + " / ".join(titles[:3]))


def find_feed(url):
    if "youtube.com" in url and not YT_CHANNEL.search(url):
        page = get(url, {"Accept-Language": "en-US,en"})[0].decode("utf-8", "replace")
        m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
             or re.search(r'"externalId":"(UC[\w-]{22})"', page))
        if not m:
            raise ValueError("no channel id on page")
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"
    if YT_CHANNEL.search(url):
        return url, None
    data, final = get(url)
    try:
        feed_title(data)
        return url, data
    except ET.ParseError:
        pass
    page = data.decode("utf-8", "replace")
    links = re.findall(r"<link[^>]+>", page, re.I)
    hrefs = [re.search(r'href=["\']([^"\']+)', l, re.I) for l in links
             if re.search(r"application/(rss|atom)\+xml", l, re.I)]
    guesses = [urljoin(final, h.group(1)) for h in hrefs if h]
    guesses += [urljoin(final, p) for p in ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml")]
    for g in guesses:
        try:
            d, _ = get(g)
            feed_title(d)
            return g, d
        except Exception:
            continue
    raise ValueError("no feed found")


def probe(url):
    try:
        feed_url, data = find_feed(url)
        yt = YT_CHANNEL.search(feed_url)
        if yt:
            items = youtube_items("", yt.group(1))
            meta = youtube_channel_page(yt.group(1)).get("metadata", {}).get("channelMetadataRenderer", {})
            title = meta.get("title", "")
        else:
            items, title = parse_feed("", data), feed_title(data)
    except Exception as e:
        return {"input": url, "error": f"{type(e).__name__}: {e}"}
    dates = sorted((i["published"] for i in items if i["published"]), reverse=True)
    per_week = (len(dates) - 1) * 7 / max((dates[0] - dates[-1]).days, 1) if len(dates) >= 2 else None
    return {"input": url, "feed_url": feed_url, "title": title, "kind": "video" if yt else "text",
            "latest": dates[0].date().isoformat() if dates else None,
            "per_week": round(per_week, 1) if per_week is not None else None,
            "samples": [{"title": i["title"], "link": i["link"]} for i in items[:6]]}


def cmd_probe(args):
    with ThreadPoolExecutor(max_workers=8) as pool:
        print(json.dumps(list(pool.map(probe, args.urls)), ensure_ascii=False, indent=1))


def cmd_discover_due(_):
    last = load(DISCOVERY, {}).get("last")
    print("yes" if last != now().date().isoformat() else f"no (already ran {last})")


def cmd_suggest(args):
    """Add new scout picks as pending suggestions, skipping anything known. --add follows them right away."""
    sugg = load(SUGGESTIONS, [])
    known = {s["feed_url"] for s in sugg} | {o.get("xmlUrl") for o in outlines()}
    added = 0
    for c in load(Path(args.file), []):
        if c["feed_url"] in known:
            continue
        known.add(c["feed_url"])
        s = {"id": item_id(c["feed_url"], ""), "status": "pending", "suggested_at": now().date().isoformat(),
             **{k: c.get(k) for k in ("name", "site", "feed_url", "format", "interest", "why", "per_week", "samples")}}
        sugg.append(s)
        if args.add:
            decide(sugg, s["id"], "add")
        added += 1
    save(SUGGESTIONS, sugg)
    save(DISCOVERY, {"last": now().date().isoformat()})
    if args.add:
        print(f"{added} feeds added; following {len(outlines())}")
    else:
        print(f"{added} new suggestions; {sum(s['status'] == 'pending' for s in sugg)} pending")


# The page stores votes, saved items and feed decisions through this server, one JSON file per collection.
LOCK = threading.Lock()


class PageHandler(BaseHTTPRequestHandler):
    def reply(self, code, body, kind="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(self):
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
        if len(parts) in (2, 3) and parts[0] == "api" and parts[1] in COLLECTIONS:
            return STATE / f"{parts[1]}.json", parts[2] if len(parts) == 3 else None
        return None, None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/feed")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if path == "/feed":
            if PAGE.exists():
                return self.reply(200, PAGE.read_bytes(), "text/html")
            return self.reply(200, "<p>No paper yet. Run /feed in your agent CLI.</p>", "text/html")
        if path == "/api/ping":
            return self.reply(200, json.dumps({"app": "newspaper", "pid": os.getpid(), "version": VERSION,
                                               "writable": can_write()}))
        file, doc_id = self.route()
        if not file or doc_id:
            return self.reply(404, "{}")
        with LOCK:
            self.reply(200, json.dumps(load(file, {}), ensure_ascii=False))

    def do_PUT(self):
        file, doc_id = self.route()
        if not doc_id:
            return self.reply(404, "{}")
        try:
            doc = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            return self.reply(400, "{}")
        with LOCK:
            docs = load(file, {})
            docs[doc_id] = doc
            save(file, docs)
        self.reply(200, "{}")

    def do_DELETE(self):
        file, doc_id = self.route()
        if not doc_id:
            return self.reply(404, "{}")
        with LOCK:
            docs = load(file, {})
            docs.pop(doc_id, None)
            save(file, docs)
        self.reply(200, "{}")

    def log_message(self, *args):
        pass


def can_write():
    """False when the server was started inside an agent's sandbox, where it can't save the page's clicks."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / ".write_test").write_text("ok", encoding="utf-8")
        return True
    except OSError:
        return False


def server_info(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            info = json.loads(r.read())
            return info if info.get("app") == "newspaper" else None
    except Exception:
        return None


def port_busy(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def page_url(port):
    return f"http://{HOST}:{port}/feed"


def cmd_serve(args):
    print(f"serving {page_url(args.port)}  (Ctrl+C to stop)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), PageHandler).serve_forever()


def cmd_open(args):
    url = page_url(args.port)
    info = server_info(args.port)
    if info and (info.get("version") != VERSION or not info.get("writable", True)):
        if not info.get("pid"):
            sys.exit(f"an old newspaper server is running on port {args.port}; stop it (or restart the computer) and run this again")
        os.kill(info["pid"], signal.SIGTERM)
        for _ in range(50):
            if not port_busy(args.port):
                break
            time.sleep(0.1)
        info = None
    if not info:
        if port_busy(args.port):
            sys.exit(f"port {args.port} is used by another program; try --port {args.port + 1}")
        cmd = [sys.executable, str(ROOT / "newspaper.py"), "serve", "--port", str(args.port)]
        quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, **quiet)
        else:
            subprocess.Popen(cmd, start_new_session=True, **quiet)
        for _ in range(50):
            if server_info(args.port):
                break
            time.sleep(0.1)
        else:
            sys.exit("the page server did not start; run `python newspaper.py serve` to see why")
    if not args.no_browser:
        webbrowser.open(url)
    print(url)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    it = sub.add_parser("init")
    it.add_argument("--opml")
    sub.add_parser("sections")
    sub.add_parser("fetch")
    b = sub.add_parser("batches")
    b.add_argument("stage", choices=["triage", "read"])
    b.add_argument("--size", type=int)
    sub.add_parser("render")
    sub.add_parser("commit")
    v = sub.add_parser("votes")
    v.add_argument("--mark", action="store_true")
    sub.add_parser("apply-decisions")
    sub.add_parser("signals")
    sub.add_parser("discover-due")
    pr = sub.add_parser("probe")
    pr.add_argument("urls", nargs="+")
    sg = sub.add_parser("suggest")
    sg.add_argument("file")
    sg.add_argument("--add", action="store_true")
    for name in ("serve", "open"):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=PORT)
        if name == "open":
            p.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    {"init": cmd_init, "sections": cmd_sections, "fetch": cmd_fetch, "batches": cmd_batches, "render": cmd_render, "commit": cmd_commit,
     "votes": cmd_votes, "apply-decisions": cmd_apply_decisions, "signals": cmd_signals, "probe": cmd_probe,
     "discover-due": cmd_discover_due, "suggest": cmd_suggest, "serve": cmd_serve, "open": cmd_open}[args.cmd](args)


if __name__ == "__main__":
    main()
