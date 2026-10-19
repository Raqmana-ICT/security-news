#!/usr/bin/env python3
"""Fetch the security news feeds in feeds.json and write news.json for raqmana.ly's news pages.

    python fetch_news.py                       # writes site/news.json, keeps conditional-GET state in state.json
    python fetch_news.py --out news.json --state /tmp/state.json

Stored per item: headline, source, date, link to the original, and an excerpt of at most 30 words taken
from the feed's own summary. Nothing else: no article text, no images, no scraping, no rewriting.

Exit status 1 (so the workflow does not deploy and the last good news.json stays live) when fewer than
half of the enabled feeds succeed, or when no items are left.
"""
import argparse
import difflib
import hashlib
import html
import json
import logging
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
USER_AGENT = "RaqmanaSecurityNews/1.0 (+https://raqmana.ly/en/blog/)"
TIMEOUT = 10             # seconds, per request
MAX_BYTES = 5_000_000    # a feed larger than this is refused
PER_FEED = 15            # newest items kept per feed
MAX_ITEMS = 80           # items in news.json
EXCERPT_WORDS = 30
TITLE_SIMILARITY = 0.9   # difflib ratio at or above which two headlines are the same story

log = logging.getLogger("fetch_news")


class FeedError(Exception):
    """A feed that could not be fetched or parsed. Logged and skipped, never fatal on its own."""


# ---------------------------------------------------------------- text
class _TextExtractor(HTMLParser):
    """HTML -> plain text with a real parser. Script/style content is dropped; block tags become spaces."""
    SKIP = {"script", "style", "noscript", "template", "iframe", "object", "svg", "math", "head", "title"}
    BLOCK = {"p", "br", "div", "li", "ul", "ol", "tr", "td", "th", "blockquote", "figure", "figcaption",
             "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "img", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


# Publisher boilerplate at the start or end of feed summaries
_BOILERPLATE = [
    re.compile(r"^View CSAF(?: Summary)?\s*", re.I),                                   # CISA advisories
    re.compile(r"\s*The post .{1,300}? appeared first on .{1,120}?\.?\s*$", re.S | re.I),
    # a trailing call to action after the last sentence, e.g. "… flaw. Continue reading Big Flaw →"
    re.compile(r"(?:(?<=[.!?…\"'”»])|^)\s*(?:Continue reading|Read more|Read the full (?:story|article))\b"
               r"[^.!?]{0,120}?\s*(?:→|»|›|…|\.\.\.)?\s*$"),
    re.compile(r"\s*\[(?:…|\.\.\.|&hellip;)\]\s*$"),
]
_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍⁠﻿"), None)


def clean_text(value):
    """Strip HTML, unescape entities (twice, for double-escaped feeds), drop boilerplate, collapse whitespace."""
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(str(value))
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # noqa: BLE001 - malformed markup: fall back to removing anything tag-shaped
        text = re.sub(r"<[^>]*>", " ", str(value))
    text = html.unescape(text).translate(_INVISIBLE)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern in _BOILERPLATE:
        text = pattern.sub("", text).strip()
    return text


def make_excerpt(text, limit=EXCERPT_WORDS):
    """At most `limit` words; '…' only when we cut it."""
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip(" ,;:–—-") + "…"


# ---------------------------------------------------------------- links
_TRACKING = re.compile(r"^(?:utm_[a-z0-9_]*|fbclid|gclid|dclid|gbraid|wbraid|msclkid|yclid|mc_cid|mc_eid|_hsenc|_hsmi|"
                       r"mkt_tok|igshid|ref|ref_src|ncid|cmpid|rss|feed|ito|sr_share)$", re.I)


def safe_url(value):
    """The URL if it is a plain http(s) link to a host, else None (blocks javascript:, data:, vbscript:, ...)."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > 2048 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host or parts.username or parts.password:
        return None
    return url


def normalize_link(url):
    """Comparable form of a link: no scheme, no www., no tracking parameters, no trailing slash, no fragment."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/+$", "", parts.path) or ""
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k))
    return host + path + ("?" + urlencode(query) if query else "")


# ---------------------------------------------------------------- regions
_AR_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # harakat, Quranic marks, tatweel
_AR_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})
_AR_LETTER = r"ء-يٮ-ۓۺ-ۿ"


def normalize_arabic(text):
    return _AR_DIACRITICS.sub("", text).translate(_AR_ALEF)


class RegionTagger:
    """Tags text as one of regions.json's regions (in priority order), else 'Global'."""

    def __init__(self, config):
        self.order = config["order"]
        prefixes = "|".join(sorted(map(re.escape, config["ar_prefixes"]), key=len, reverse=True))
        suffixes = "|".join(sorted(map(re.escape, config["ar_suffixes"]), key=len, reverse=True))
        self.rules = {}
        for name in self.order:
            spec = config["regions"][name]
            self.rules[name] = {
                "en": self._english(spec.get("en", [])),
                "en_exclude": self._english(spec.get("en_exclude", []), plural=False),
                "ar": self._arabic(spec.get("ar_words", []), spec.get("ar_stems", []), prefixes, suffixes),
                "ar_exclude": self._arabic(spec.get("ar_exclude", []), [], prefixes, suffixes),
            }

    @staticmethod
    def _english(terms, plural=True):
        if not terms:
            return None
        alts = sorted((r"\s+".join(map(re.escape, t.split())) for t in terms), key=len, reverse=True)
        return re.compile(r"\b(?:" + "|".join(alts) + r")" + ("s?" if plural else "") + r"\b", re.I)

    @staticmethod
    def _arabic(words, stems, prefixes, suffixes):
        def alt(terms):
            return "|".join(sorted((r"\s+".join(map(re.escape, normalize_arabic(t).split())) for t in terms),
                                   key=len, reverse=True))
        branches = []
        if words:
            branches.append(f"(?:{alt(words)})(?:{suffixes})?")
        if stems:
            branches.append(f"(?:{alt(stems)})(?:{suffixes})")
        if not branches:
            return None
        return re.compile(f"(?<![{_AR_LETTER}])(?:{prefixes})?(?:{'|'.join(branches)})(?![{_AR_LETTER}])")

    def tag(self, text):
        text = normalize_arabic(text or "")
        for name in self.order:
            rule, probe = self.rules[name], text
            for exclude in (rule["en_exclude"], rule["ar_exclude"]):
                if exclude:
                    probe = exclude.sub(" ", probe)
            if any(rx and rx.search(probe) for rx in (rule["en"], rule["ar"])):
                return name
        return "Global"


# ---------------------------------------------------------------- feeds
def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_date(entry, now):
    """(UTC datetime, estimated?) - the fetch time when the entry has no usable date; never in the future."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                dt = datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if dt.year < 2000:
                continue
            return min(dt, now), False
    return now, True


def entry_to_item(entry, feed, now, tagger):
    """One feed entry -> one news item, or None if it has no valid http(s) link or no headline."""
    url = safe_url(entry.get("link"))
    title = clean_text(entry.get("title"))[:300]
    if not url or not title:
        return None
    summary = clean_text(entry.get("summary") or entry.get("description") or "")
    excerpt = "" if summary.casefold() == title.casefold() else make_excerpt(summary)
    published, estimated = entry_date(entry, now)
    return {
        "id": hashlib.sha1(normalize_link(url).encode("utf-8")).hexdigest()[:16],
        "title": title,
        "url": url,
        "source": feed["name"],
        "type": feed["type"],
        "lang": feed["lang"],
        "region": tagger.tag(f"{title} {excerpt}"),
        "published": iso(published),
        "date_estimated": estimated,
        "excerpt": excerpt,
        "also_reported_by": [],
    }


def parse_feed(content, feed, now, tagger):
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise FeedError(f"malformed feed: {type(parsed.bozo_exception).__name__}")
    items = [i for i in (entry_to_item(e, feed, now, tagger) for e in parsed.entries) if i]
    if not items:
        raise FeedError("no usable items")
    items.sort(key=lambda i: i["published"], reverse=True)
    return items[:PER_FEED]


def download(session, url, headers):
    """GET with a 10 s timeout and one retry on network errors or 5xx; refuses bodies over MAX_BYTES."""
    for attempt in (1, 2):
        try:
            with session.get(url, headers=headers, timeout=TIMEOUT, stream=True) as r:
                if r.status_code >= 500 and attempt == 1:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                body = bytearray()
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise FeedError("feed larger than 5 MB")
                return r.status_code, r.headers, bytes(body)
        except (requests.RequestException, requests.HTTPError) as exc:
            if attempt == 2:
                raise FeedError(f"network error: {type(exc).__name__}") from exc
            log.info("retrying %s after %s", url, exc)
            time.sleep(2)


def fetch_feed(session, feed, cached, now, tagger):
    """(items, state entry, how) for one feed; conditional GET, reusing cached items on 304."""
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"}
    if cached.get("items"):
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
    status, resp_headers, body = download(session, feed["url"], headers)
    if status == 304 and cached.get("items"):
        return cached["items"], cached, "not modified"
    if status != 200:
        raise FeedError(f"HTTP {status}")
    items = parse_feed(body, feed, now, tagger)
    entry = {"etag": resp_headers.get("ETag"), "last_modified": resp_headers.get("Last-Modified"), "items": items}
    return items, entry, "fetched"


# ---------------------------------------------------------------- de-duplication
def _title_key(title):
    t = unicodedata.normalize("NFKC", title).casefold()
    t = "".join(" " if unicodedata.category(ch).startswith(("P", "S")) else ch for ch in t)
    return re.sub(r"\s+", " ", t).strip()


def _merge(keep, other):
    seen = {keep["source"]} | {r["source"] for r in keep["also_reported_by"]}
    for rep in [{"source": other["source"], "url": other["url"]}] + other["also_reported_by"]:
        if rep["source"] not in seen:
            keep["also_reported_by"].append(rep)
            seen.add(rep["source"])


def dedupe(items):
    """Same link (normalised) or near-identical headline -> one item: the earliest dated copy, crediting the rest."""
    ordered = sorted(items, key=lambda i: (i["date_estimated"], i["published"]))   # earliest real dates first
    kept, by_link, keys = [], {}, []
    for item in ordered:
        link = normalize_link(item["url"])
        if link in by_link:
            _merge(by_link[link], item)
            continue
        key = _title_key(item["title"])
        match = None
        for other, other_key in zip(kept, keys):
            sm = difflib.SequenceMatcher(None, key, other_key)
            if sm.real_quick_ratio() >= TITLE_SIMILARITY and sm.quick_ratio() >= TITLE_SIMILARITY \
                    and sm.ratio() >= TITLE_SIMILARITY:
                match = other
                break
        if match:
            _merge(match, item)
            by_link[link] = match
            continue
        kept.append(item)
        keys.append(key)
        by_link[link] = item
    return kept


# ---------------------------------------------------------------- run
def run(feeds, tagger, state, session, now):
    enabled = [f for f in feeds if f.get("enabled")]
    ok, failed, items, new_state, how = [], [], [], {}, {}
    for feed in enabled:
        try:
            got, entry, how[feed["name"]] = fetch_feed(session, feed, state.get(feed["url"], {}), now, tagger)
        except FeedError as exc:
            log.warning("skipped %s: %s", feed["name"], exc)
            failed.append({"name": feed["name"], "error": str(exc)})
            if feed["url"] in state:   # keep its cache for the next run
                new_state[feed["url"]] = state[feed["url"]]
            continue
        ok.append(feed["name"])
        new_state[feed["url"]] = entry
        # re-tag cached items too, so edits to regions.json apply on the next run
        for item in got:
            item = dict(item, also_reported_by=[])
            item["region"] = tagger.tag(f"{item['title']} {item['excerpt']}")
            items.append(item)
    merged = dedupe(items)
    merged.sort(key=lambda i: i["published"], reverse=True)
    news = {"updated": iso(now), "sources_ok": ok, "sources_failed": failed, "items": merged[:MAX_ITEMS]}
    return news, new_state, enabled, how


def summary_table(news, how):
    by_source = Counter(i["source"] for i in news["items"])
    by_region = Counter(i["region"] for i in news["items"])
    lines = ["", f"{'Source':32} {'Items':>5}  Status", f"{'-' * 32} {'-' * 5}  {'-' * 12}"]
    for name in news["sources_ok"]:
        lines.append(f"{name:32} {by_source.get(name, 0):>5}  {how.get(name, '')}")
    for f in news["sources_failed"]:
        lines.append(f"{f['name']:32} {'-':>5}  FAILED: {f['error']}")
    lines += ["", f"{'Region':32} {'Items':>5}", f"{'-' * 32} {'-' * 5}"]
    for region in ("Libya", "MENA", "Global"):
        lines.append(f"{region:32} {by_region.get(region, 0):>5}")
    lines.append(f"{'Total (after de-duplication)':32} {len(news['items']):>5}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch security news feeds into news.json.")
    ap.add_argument("--feeds", default=ROOT / "feeds.json", type=Path)
    ap.add_argument("--regions", default=ROOT / "regions.json", type=Path)
    ap.add_argument("--out", default=ROOT / "site" / "news.json", type=Path)
    ap.add_argument("--state", default=ROOT / "state.json", type=Path, help="conditional-GET cache (ETag, Last-Modified, items)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    feeds = json.loads(args.feeds.read_text(encoding="utf-8"))
    tagger = RegionTagger(json.loads(args.regions.read_text(encoding="utf-8")))
    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with requests.Session() as session:
        news, new_state, enabled, how = run(feeds, tagger, state, session, now)

    args.state.write_text(json.dumps(new_state, ensure_ascii=False), encoding="utf-8")
    print(summary_table(news, how))
    if len(news["sources_ok"]) * 2 < len(enabled) or not news["items"]:
        log.error("safety net: %d of %d feeds succeeded, %d items; news.json NOT written",
                  len(news["sources_ok"]), len(enabled), len(news["items"]))
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(news, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(args.out)
    log.info("wrote %s (%d items)", args.out, len(news["items"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
