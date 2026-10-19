# security-news

Pipeline behind the raqmana.ly news pages: `fetch_news.py` → `site/news.json` → GitHub Pages, hourly via `.github/workflows/update.yml`. See README.md for usage.

## Rules

### Copyright
- Store only the headline, source name, date, link, and an excerpt of at most 30 words from the feed's own summary.
- Never store full text or images, never scrape article pages, and never rewrite or summarise articles with AI.
- Every item credits and links to its source.

### Security
- Accept only `http:`/`https:` links (`safe_url`); drop the item otherwise.
- Strip HTML with a real parser.
- Never publish data carrying a `_fixture` key. The workflow checks for it.

### GitHub Actions
- Use least-privilege `permissions:`: the build job has `contents: read`; only the deploy job has `pages: write` and `id-token: write`.
- Pin every action to a full commit SHA, with a version comment.
- Keep the `concurrency` group.

### Simplicity
- Python 3 with `feedparser` and `requests` only, all versions pinned in `requirements.txt`.
- Run the tests (`python -m pytest`) before every commit.

### Sources
- Don't add or enable a source without the owner's OK.
- Proposed but not enabled:
  - SOCRadar
  - Kaspersky Securelist
  - Cybercast (Arabic)
  - AITNews (Arabic)
  - Central Bank of Libya `cbl.gov.ly/feed/`
  - Libya Herald
  - NISSA `nissa.gov.ly/feed/`. Its newest post is from 2024; enable it only with the 60-day age cap, which applies anyway.
