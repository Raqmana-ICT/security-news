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
                         "date_estimated", "excerpt", "also_reported_by"}               # nothing beyond the allowed fields


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
def _item(title, url, source, published, estimated=False):
    return {"id": url, "title": title, "url": url, "source": source, "type": "Global", "lang": "en", "region": "Global",
            "published": published, "date_estimated": estimated, "excerpt": "", "also_reported_by": []}


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
