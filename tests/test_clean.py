"""Text cleaning, excerpts, link validation, de-duplication and dates."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fetch_news as fn

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def tagger():
    return fn.RegionTagger(json.loads((ROOT / "regions.json").read_text(encoding="utf-8")))


FEED = {"name": "Test Feed", "url": "https://feed.example/rss", "type": "Global", "lang": "en", "enabled": True}


# ---------------------------------------------------------------- text
def test_clean_text_strips_html_and_scripts():
    raw = '<p>Hello <b>world</b><script>alert(1)</script><style>p{}</style></p><img src="x" onerror="y">done'
    assert fn.clean_text(raw) == "Hello world done"


def test_clean_text_unescapes_entities_including_double_escaped():
    assert fn.clean_text("AT&amp;T &#8217;s &lt;b&gt;") == "AT&T ’s <b>"
    assert fn.clean_text("Fish &amp;amp; chips") == "Fish & chips"


def test_clean_text_removes_publisher_boilerplate():
    raw = "<p>Attackers abused a flaw.</p><p>The post Big Flaw appeared first on Example News.</p>"
    assert fn.clean_text(raw) == "Attackers abused a flaw."
    assert fn.clean_text("Attackers abused a flaw. [&#8230;]") == "Attackers abused a flaw."
    assert fn.clean_text("<p>View CSAF</p><h2>Summary</h2><p>Successful exploitation could allow</p>") \
        == "Successful exploitation could allow"
    assert fn.clean_text("Read more about how we read more.") == "Read more about how we read more."  # only trailing boilerplate
    assert fn.clean_text("Attackers abused a flaw. Continue reading Big Flaw →") == "Attackers abused a flaw."
    assert fn.clean_text("Attackers abused a flaw. Read more…") == "Attackers abused a flaw."


def test_clean_text_collapses_whitespace_and_invisible_chars():
    assert fn.clean_text("  a​\n\n b\t\tc  ") == "a b c"


def test_excerpt_limits_to_30_words_and_marks_cut():
    words = [f"w{i}" for i in range(40)]
    out = fn.make_excerpt(" ".join(words))
    assert out.endswith("…")
    assert len(out.rstrip("…").split()) == 30


def test_excerpt_untouched_when_short():
    text = " ".join(f"w{i}" for i in range(30))
    assert fn.make_excerpt(text) == text          # exactly 30: no ellipsis
    assert fn.make_excerpt("") == ""


# ---------------------------------------------------------------- links
@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "JavaScript:alert(1)", " javascript:alert(1)", "data:text/html,<script>x</script>",
    "vbscript:msgbox", "ftp://example.com/a", "//example.com/a", "/relative/path", "https://", "https:///nohost",
    "https://user:pass@example.com/", "https://good.com@evil.com/", "https://exa mple.com/", "https://example.com/\nx",
    None, 42, "",
])
def test_safe_url_rejects(url):
    assert fn.safe_url(url) is None


@pytest.mark.parametrize("url", ["https://example.com/a?b=c#d", "http://example.com", "HTTPS://Example.com/x"])
def test_safe_url_accepts(url):
    assert fn.safe_url(url) == url


def test_normalize_link():
    a = fn.normalize_link("https://www.Example.com/news/story/?utm_source=rss&utm_medium=feed&id=7#top")
    b = fn.normalize_link("http://example.com/news/story?id=7&fbclid=abc")
    assert a == b == "example.com/news/story?id=7"


# ---------------------------------------------------------------- feed parsing
RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Good &amp;amp; dated</title><link>https://ex.com/a?utm_source=rss</link>
  <pubDate>Sat, 18 Oct 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;Summary &lt;b&gt;bold&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Evil link</title><link>javascript:alert(1)</link><pubDate>Sat, 18 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Data link</title><link>data:text/html;base64,PHNjcmlwdD4=</link></item>
<item><title>From the future</title><link>https://ex.com/future</link><pubDate>Tue, 01 Jan 2030 00:00:00 GMT</pubDate></item>
<item><title>No date at all</title><link>https://ex.com/nodate</link></item>
<item><title></title><link>https://ex.com/notitle</link></item>
</channel></rss>"""


def test_parse_feed(tagger):
    items = fn.parse_feed(RSS.encode(), FEED, NOW, tagger)
    by_title = {i["title"]: i for i in items}
    assert set(by_title) == {"Good & dated", "From the future", "No date at all"}   # unsafe links and empty title dropped
    good = by_title["Good & dated"]
    assert good["excerpt"] == "Summary bold"
    assert good["published"] == "2026-10-18T10:00:00Z" and good["date_estimated"] is False
    assert by_title["From the future"]["published"] == fn.iso(NOW)                 # clamped
    assert by_title["No date at all"]["date_estimated"] is True
    assert by_title["No date at all"]["published"] == fn.iso(NOW)
    assert set(good) == {"id", "title", "url", "source", "type", "lang", "region", "published",
                         "date_estimated", "excerpt", "also_reported_by", "cves", "kev"}   # nothing beyond the allowed fields


def test_parse_feed_rejects_garbage(tagger):
    with pytest.raises(fn.FeedError):
        fn.parse_feed(b"<html><body>not a feed</body></html>", FEED, NOW, tagger)


def test_per_feed_limit(tagger):
    items = "".join(f"<item><title>Story {i}</title><link>https://ex.com/{i}</link>"
                    f"<pubDate>Sat, 18 Oct 2026 {i % 24:02d}:00:00 GMT</pubDate></item>" for i in range(30))
    out = fn.parse_feed(f"<rss version='2.0'><channel><title>T</title>{items}</channel></rss>".encode(), FEED, NOW, tagger)
    assert len(out) == fn.PER_FEED
    assert out == sorted(out, key=lambda i: i["published"], reverse=True)


# ---------------------------------------------------------------- de-duplication
def _item(title, url, source, published, estimated=False, cves=()):
    return {"id": url, "title": title, "url": url, "source": source, "type": "Global", "lang": "en", "region": "Global",
            "published": published, "date_estimated": estimated, "excerpt": "", "also_reported_by": [],
            "cves": list(cves), "kev": False}


def test_dedupe_by_link_keeps_earliest_and_credits_others():
    items = [_item("Story A", "https://www.ex.com/a/?utm_source=x", "Late", "2026-10-18T12:00:00Z"),
             _item("Story A (update)", "http://ex.com/a", "Early", "2026-10-18T09:00:00Z")]
    out = fn.dedupe(items)
    assert len(out) == 1
    assert out[0]["source"] == "Early"
    assert out[0]["also_reported_by"] == [{"source": "Late", "url": "https://www.ex.com/a/?utm_source=x"}]


def test_dedupe_by_near_identical_title():
    items = [_item("Microsoft patches 63 flaws, including two zero-days!", "https://a.com/1", "A", "2026-10-18T10:00:00Z"),
             _item("Microsoft Patches 63 Flaws Including Two Zero-Days", "https://b.com/2", "B", "2026-10-18T08:00:00Z"),
             _item("Apple patches Safari zero-day", "https://c.com/3", "C", "2026-10-18T07:00:00Z")]
    out = fn.dedupe(items)
    assert len(out) == 2
    ms = next(i for i in out if "Microsoft" in i["title"])
    assert ms["source"] == "B" and [r["source"] for r in ms["also_reported_by"]] == ["A"]


def test_dedupe_prefers_real_dates_over_estimated():
    items = [_item("Same story", "https://a.com/x", "Undated", "2026-10-18T01:00:00Z", estimated=True),
             _item("Same story", "https://b.com/y", "Dated", "2026-10-18T05:00:00Z")]
    assert fn.dedupe(items)[0]["source"] == "Dated"


# ---------------------------------------------------------------- run / safety net
class _FakeSession:
    """Serves canned responses per URL instead of the network."""
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, **kw):
        status, body = self.responses[url]
        return _FakeResponse(status, body)


class _FakeResponse:
    def __init__(self, status, body):
        self.status_code, self.body, self.headers = status, body, {"ETag": '"v1"'}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        yield self.body


def _feeds(n):
    return [dict(FEED, name=f"F{i}", url=f"https://f{i}.example/rss") for i in range(n)]


def test_run_skips_broken_feeds(tagger, monkeypatch):
    monkeypatch.setattr(fn.time, "sleep", lambda s: None)
    feeds = _feeds(3)
    session = _FakeSession({feeds[0]["url"]: (200, RSS.encode()), feeds[1]["url"]: (200, b"garbage"),
                            feeds[2]["url"]: (404, b"")})
    news, state, enabled, _ = fn.run(feeds, tagger, {}, session, NOW)
    assert news["sources_ok"] == ["F0"]
    assert [f["name"] for f in news["sources_failed"]] == ["F1", "F2"]
    assert len(news["items"]) == 3
    assert list(state) == [feeds[0]["url"]]


def test_run_reuses_cached_items_on_304(tagger):
    feed = _feeds(1)[0]
    cached = {feed["url"]: {"etag": '"v1"', "last_modified": None,
                            "items": [_item("Cached story", "https://ex.com/c", "F0", "2026-10-18T10:00:00Z")]}}
    news, _, _, how = fn.run([feed], tagger, cached, _FakeSession({feed["url"]: (304, b"")}), NOW)
    assert [i["title"] for i in news["items"]] == ["Cached story"] and how["F0"] == "not modified"


def test_main_safety_net_exits_nonzero_and_keeps_old_file(tmp_path, monkeypatch):
    feeds = _feeds(4)
    (tmp_path / "feeds.json").write_text(json.dumps(feeds))
    out = tmp_path / "news.json"
    out.write_text('{"last": "good"}')
    responses = {f["url"]: (500, b"") for f in feeds}
    responses[feeds[0]["url"]] = (200, RSS.encode())                     # 1 of 4 succeeds: below half
    monkeypatch.setattr(fn.requests, "Session", lambda: _CtxSession(responses))
    monkeypatch.setattr(fn.time, "sleep", lambda s: None)
    code = fn.main(["--feeds", str(tmp_path / "feeds.json"), "--out", str(out), "--state", str(tmp_path / "s.json")])
    assert code == 1
    assert out.read_text() == '{"last": "good"}'


class _CtxSession(_FakeSession):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_future_and_old_dates():
    entry = {"published_parsed": (NOW + timedelta(days=3)).timetuple()}
    assert fn.entry_date(entry, NOW) == (NOW, False)
    assert fn.entry_date({"published_parsed": (1970, 1, 1, 0, 0, 0, 0, 1, 0)}, NOW) == (NOW, True)


# ---------------------------------------------------------------- filters, CVE grouping, KEV
@pytest.fixture(scope="module")
def filt():
    return fn.ItemFilter(json.loads((ROOT / "filters.json").read_text(encoding="utf-8")))


TRADE = {"name": "Trade", "url": "u", "type": "Regional", "lang": "en", "require_any": ["threat"], "exclude_any": ["press_release"]}
CISA = {"name": "CISA", "url": "u", "type": "Global", "lang": "en", "exclude_any": ["us_elections"], "exclude_url": ["/ics-medical-advisories/"]}


@pytest.mark.parametrize("title, keep", [
    ("Ransomware gang hits Gulf logistics firm", True),
    ("Attackers exploit flaw in UAE government portal", True),
    ("AI, security and technology sovereignty reshape enterprise priorities", False),   # no threat named
    ("Vendor appoints new regional VP after record breach-response year", False),     # press release
    ("Hamad International Airport launches biometric boarding", False),
    ("Company unveils ransomware protection suite", False),                           # PR even with a threat word
])
def test_trade_press_gate(filt, title, keep):
    item = _item(title, "https://x.example/a", "Trade", "2026-10-18T10:00:00Z")
    assert filt.keep(item, TRADE) is keep


def test_cisa_exclusions(filt):
    medical = _item("Thermo Fisher Torrent Suite", "https://www.cisa.gov/news-events/ics-medical-advisories/icsma-26-1", "CISA", "2026-10-18T10:00:00Z")
    election = _item("Cyber Threat Actors Target Election and Campaign Officials", "https://www.cisa.gov/a", "CISA", "2026-10-18T10:00:00Z")
    ics = _item("Siemens SCALANCE flaws", "https://www.cisa.gov/news-events/ics-advisories/icsa-26-1", "CISA", "2026-10-18T10:00:00Z")
    assert not filt.keep(medical, CISA) and not filt.keep(election, CISA) and filt.keep(ics, CISA)
    assert fn.ALLOW_ALL.keep(medical, {})                                             # feeds without rules keep everything


def test_same_cve_is_one_story():
    items = [_item("BeyondTrust zero-day exploited", "https://a.com/1", "A", "2026-10-18T10:00:00Z", cves=["CVE-2026-102764"]),
             _item("CISA adds BeyondTrust flaw to KEV", "https://b.com/2", "B", "2026-10-18T08:00:00Z", cves=["CVE-2026-102764", "CVE-2026-1"]),
             _item("Unrelated Fortinet bug", "https://c.com/3", "C", "2026-10-18T07:00:00Z", cves=["CVE-2025-61624"])]
    out = fn.dedupe(items)
    assert len(out) == 2
    bt = next(i for i in out if "CVE-2026-102764" in i["cves"])
    assert bt["source"] == "B" and [r["source"] for r in bt["also_reported_by"]] == ["A"]
    assert bt["cves"] == ["CVE-2026-1", "CVE-2026-102764"]


def test_cves_in():
    assert fn.cves_in("Patch cve-2026-102764 and CVE-2025-61624 now; CVE-2025-61624 again") == ["CVE-2025-61624", "CVE-2026-102764"]
    assert fn.cves_in("no ids here, CVE-26-1") == []


def test_kev_flag(tagger):
    feed = _feeds(1)[0]
    cached = {feed["url"]: {"etag": None, "last_modified": None, "items": [
        dict(_item("Exploited bug", "https://ex.com/k", "F0", "2026-10-18T10:00:00Z"), excerpt="CVE-2026-102764 is under attack", cves=[]),
        _item("Other bug", "https://ex.com/o", "F0", "2026-10-18T09:00:00Z", cves=["CVE-2020-1"])]}}
    news, *_ = fn.run([feed], tagger, cached, _FakeSession({feed["url"]: (304, b"")}), NOW, kev=frozenset({"CVE-2026-102764"}))
    flags = {i["title"]: i["kev"] for i in news["items"]}
    assert flags == {"Exploited bug": True, "Other bug": False}


def test_cisa_table_junk_removed():
    assert fn.clean_text("Successful exploitation could allow access. CVSS Vendor Equipment v3 8.1 CISA Malcolm 6 Vuln") \
        == "Successful exploitation could allow access."


def test_stories_older_than_the_cap_are_dropped(tagger):
    feed = _feeds(1)[0]
    cached = {feed["url"]: {"etag": None, "last_modified": None, "items": [
        _item("Fresh", "https://ex.com/f", "F0", "2026-10-18T10:00:00Z"),
        _item("Ancient", "https://ex.com/a", "F0", "2025-01-01T10:00:00Z")]}}
    news, *_ = fn.run([feed], tagger, cached, _FakeSession({feed["url"]: (304, b"")}), NOW)
    assert [i["title"] for i in news["items"]] == ["Fresh"]


def test_feed_level_region_and_cyber_gate(tagger, filt):
    feed = dict(FEED, region="Libya", require_any=["cyber"], lang="ar")
    rss = """<rss version="2.0"><channel><title>CBL</title>
<item><title>المحافظ يبحث تعزيز التعاون في المدفوعات</title><link>https://cbl.gov.ly/1</link><pubDate>Sat, 18 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>تحذير من صفحات وهمية تنتحل صفة المصرف</title><link>https://cbl.gov.ly/2</link><pubDate>Sat, 18 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Ceasefire breach reported near Sirte</title><link>https://cbl.gov.ly/3</link><pubDate>Sat, 18 Oct 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""
    items = fn.parse_feed(rss.encode(), feed, NOW, tagger, filt)
    assert [i["title"] for i in items] == ["تحذير من صفحات وهمية تنتحل صفة المصرف"]
    assert items[0]["region"] == "Libya"


def test_dates_without_time_zone_are_read_as_utc():
    dt, est = fn.entry_date({"published": "Wed, 14 Oct 2026 08:01:12"}, NOW)
    assert (dt.isoformat(), est) == ("2026-10-14T08:01:12+00:00", False)
    assert fn.entry_date({"published": "not a date"}, NOW) == (NOW, True)


def test_official_feed_goes_to_its_own_list_without_age_cap(tagger):
    news_feed, nissa = _feeds(2)
    nissa = dict(nissa, name="NISSA", official=True)
    cached = {
        news_feed["url"]: {"etag": None, "last_modified": None, "items": [_item("Fresh news", "https://ex.com/n", "F0", "2026-10-18T10:00:00Z")]},
        nissa["url"]: {"etag": None, "last_modified": None, "items": [
            _item(f"NISSA post {n}", f"https://nissa.example/{n}", "NISSA", f"2024-0{n}-01T10:00:00Z") for n in range(1, 8)]},
    }
    session = _FakeSession({news_feed["url"]: (304, b""), nissa["url"]: (304, b"")})
    news, *_ = fn.run([news_feed, nissa], tagger, cached, session, NOW)
    assert [i["title"] for i in news["items"]] == ["Fresh news"]                      # never mixed into the news
    assert [o["title"] for o in news["official"]] == [f"NISSA post {n}" for n in (7, 6, 5, 4, 3)]   # newest 5, 2024 kept
    assert set(news["official"][0]) == {"title", "url", "source", "lang", "published", "date_estimated"}
    assert "NISSA" in news["sources_ok"]
