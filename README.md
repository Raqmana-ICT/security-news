# security-news

An hourly feed of cybersecurity headlines for the news pages on raqmana.ly, `/en/blog/` and `/ar/blog/`. A GitHub Actions workflow fetches the publishers' RSS feeds, writes `news.json`, and publishes it to GitHub Pages. The website copies that file onto raqmana.ly every hour with a cron job (`server/pull-news.sh` in the website repo).

Each story keeps only its headline, source, date, link, and an excerpt of up to 30 words from the feed's own summary. Nothing else is stored.

## Run it locally

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
.venv/bin/python fetch_news.py          # writes site/news.json and prints counts per source and region
```

## Add or remove a source

Edit `feeds.json`. Each source has these fields:

- `name`: the publisher name shown on the site.
- `url`: the RSS or Atom feed.
- `type`: `Global`, `Regional` or `Industry`. `Industry` shows a "Vendor" badge.
- `lang`: `en` or `ar`.
- `enabled`: set to `false` to switch the source off without deleting it.
- `note`: optional, for your own reference.

Optional filters (word lists live in `filters.json`):

- `require_any`: keep only stories that match one of these lists, e.g. `["threat"]`.
- `exclude_any`: drop stories that match any of these lists, e.g. `["press_release"]`.
- `exclude_url`: drop stories whose link contains any of these strings.

Before you enable a new feed, run the script once and check its stories in `site/news.json`.

## Region keywords

`regions.json` decides whether a story is tagged Libya, MENA or Global. Its `_help` block explains the syntax. In short:

- **English terms** match whole words.
- **Arabic terms** are written without «ال». Prefixes and suffixes are handled for you.
- **Exclusions** (`en_exclude`, `ar_exclude`) stop false matches such as "Gulf of Mexico" or "Tripoli, Lebanon".
- **MENA** looks at the headline only (`"match": "title"`).
- **Libya** looks at the headline and the excerpt. When both match, Libya wins.

Run the tests after you change it.

## What the pipeline does on each run

1. Fetches each feed with conditional GET (cached in `state.json`), applies the feed's filters, and keeps the newest 15 stories per feed.
2. Drops stories older than 60 days.
3. Merges duplicates: the same link, a near-identical headline, or the same CVE ID.
4. Flags stories naming a CVE on CISA's Known Exploited Vulnerabilities list (`kev: true`).
5. Keeps the newest 80 stories.
6. Refuses to publish if fewer than half the feeds succeed, if no stories are left, or if the data carries a `_fixture` key. When it refuses, the last good file stays live.

## The keepalive workflow

GitHub disables scheduled workflows in public repositories after 60 days without activity, and Pages deployments don't count as activity. `keepalive.yml` runs once a month and re-enables both workflows through the API. It never commits, so it can't trigger itself in a loop.

## If a feed breaks

The run logs a warning and skips that feed; the other feeds still publish. If the failure persists:

1. Check the feed URL in a browser.
2. If it has moved, update `url`. If it blocks us, set `"enabled": false` and add a `note`. Never disguise the User-Agent.
3. If more than half the feeds fail, the workflow stops deploying and the site keeps the last good `news.json`. Check the Actions log.
