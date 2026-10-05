# AIScraper

Production-grade, resilient scraper for **Yahoo Finance Stock Market News**
that uses an 8-tier extraction fallback chain, conservative rate limiting,
per-domain circuit breaking, and stores structured article data in
PostgreSQL.

> Target: <https://finance.yahoo.com/topic/stock-market-news/>

---

## Table of contents

- [Architecture](#architecture)
- [Resilience strategy (8-tier extraction)](#resilience-strategy-8-tier-extraction)
- [Rate limiting & stealth](#rate-limiting--stealth)
- [Data model](#data-model)
- [Project layout](#project-layout)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Updating selectors when Yahoo changes](#updating-selectors-when-yahoo-changes)
- [Testing](#testing)
- [Observability](#observability)
- [Limitations & honest caveats](#limitations--honest-caveats)

---

## Architecture

```
            ┌────────────────────────────────────────────────────┐
            │             APScheduler  (interval job)             │
            └─────────────────────────┬──────────────────────────┘
                                      │
       ┌──────────────────────────────┴──────────────────────────────┐
       │                                                              │
┌──────▼────────┐  queue   ┌──────────────────┐  upsert  ┌────────────▼────┐
│ Stage A       │─────────▶│ Stage B           │─────────▶│ PostgreSQL      │
│ Listing crawl │  (URLs)  │ Article pipeline  │ (ON CONFLICT DO UPDATE) │
│ (scroll/JS)   │          │ (8-tier extract)  │          │  + scrape_runs  │
└──────┬────────┘          └────────┬─────────┘          └─────────────────┘
       │                            │
       └──────────────┬─────────────┘
                      ▼
            ┌──────────────────────┐
            │  Resilient Fetcher   │
            │  • Crawl4AI + stealth│
            │  • Token bucket      │
            │  • Concurrency cap   │
            │  • Per-domain CB     │
            │  • Tenacity backoff  │
            │  • robots.txt        │
            └──────────────────────┘
```

### Two-stage crawl

- **Stage A** (listing): fetch the topic page with infinite-scroll handling,
  extract article cards, dedupe, and queue fresh URLs.
- **Stage B** (article): for each queued URL, fetch the full article and run
  the tiered extraction pipeline.

---

## Resilience strategy (8-tier extraction)

Every article runs through this priority chain. Each tier is a small,
swappable module that implements `BaseExtractor.extract(url, payload) -> ArticleData | None`.
The pipeline **merges** results — first non-empty value per field wins,
and every assignment is recorded in the `extraction_tier_used` JSONB column
so you can audit which tier saved you when the site changes.

| # | Tier | Source | Stability |
|---|------|--------|-----------|
| 1 | RSS / Atom feed | `feedparser` | Most stable (Yahoo's public feed is gone; wired in for the future) |
| 2 | JSON-LD (schema.org/NewsArticle) | inline `<script type="application/ld+json">` | Very stable |
| 3 | Crawl4AI JsonCssExtractionStrategy | `crawl4ai_schema.json` | Stable, JS-aware |
| 4 | OpenGraph / Twitter Card | `meta` tags | Stable |
| 5 | trafilatura | content-density heuristics | Robust |
| 6 | newspaper4k | news-metadata heuristics | Robust |
| 7 | BeautifulSoup4 + lxml | `selectors.yaml` | Fragile (last structural fallback) |
| 8 | LLM (optional) | provider pluggable | Last-resort, feature-flagged |

If a tier returns nothing, the next one runs. If **all** tiers fail for a
required field (`title` or `full_content`), the article is marked
`status='partial_extraction'` or `status='failed'` — the pipeline never
crashes.

Every tier logs a `WARNING` with the flag *"possible site structure change"*
when its selector returns zero matches, so silent breakage is visible.

---

## Rate limiting & stealth

Four protection layers, all configurable via `.env`:

1. **Token bucket** (`aiolimiter`) — `REQUESTS_PER_MINUTE` per run, smooths bursts.
2. **Concurrency cap** (`asyncio.Semaphore`) — `MAX_CONCURRENT_REQUESTS` in flight.
3. **Uniform jitter** — random sleep between `MIN_DELAY_SEC` and `MAX_DELAY_SEC` per fetch.
4. **Per-domain circuit breaker** — on 3 consecutive 403/429/CAPTCHA responses
   (`FAILURE_THRESHOLD`), the entire run pauses for `FAILURE_COOLDOWN_SEC` and
   auto-resumes after.

`tenacity`-style exponential backoff wraps every individual fetch
(`BACKOFF_BASE_SEC * 2^attempt`, capped at `BACKOFF_MAX_SEC`).

Stealth options in the Crawl4AI browser config:

- `user_agent` rotates from a pool of realistic, current desktop UAs.
- `viewport` randomized per session within a configurable range.
- `simulate_user=True` — human-like scroll & mouse jitter.
- `magic=True` — anti-bot magic mode.
- `respect_robots_txt` — `urllib.robotparser`-based, configurable.

Proxy plug-point: `Fetcher.set_proxy_provider(callable)`. Not implemented by
default; the interface is in place so you can drop in a residential rotator
without touching any other code.

---

## Data model

Two tables (see `app/db/models.py` + `app/db/migrations/versions/0001_initial.py`):

### `articles`

- `source_url` **unique** (deduplication key)
- `title`, `summary`, `full_content`, `full_content_markdown`
- `authors` (JSONB list)
- `published_at`, `updated_at`, `first_seen_at`, `scraped_at` (TIMESTAMPTZ)
- `category`, `tags` (text[]), `related_tickers` (text[], GIN-indexed)
- `source_publisher`, `top_image_url`, `language`
- `extraction_tier_used` (JSONB) — per-field provenance
- `status` enum: `success` / `partial_extraction` / `failed`
- `raw_html_compressed` (gzip BYTEA) — for offline reprocessing when selectors break
- `search_vector` (TSVECTOR, GIN) — full-text search
- Indexes on `published_at`, `category`, `scraped_at`, `status`, `related_tickers`, `search_vector`

### `scrape_runs`

- `run_uuid`, `stage` (listing/article), `started_at`, `ended_at`
- `pages_fetched`, `new_articles`, `duplicate_articles`, `failed_articles`,
  `rate_limit_hits`, `cooldown_events`
- `errors` (JSONB), `extra_metrics` (JSONB)

Upsert: `ON CONFLICT (source_url) DO UPDATE` — re-scrapes overwrite mutable
fields but preserve `first_seen_at`.

---

## Project layout

```
.
├── app/
│   ├── core/
│   │   ├── config.py          # pydantic-settings (all env vars)
│   │   └── logging.py         # loguru structured logs
│   ├── db/
│   │   ├── models.py          # SQLAlchemy 2.x ORM
│   │   ├── session.py         # engine + SessionLocal
│   │   └── migrations/        # alembic
│   │       ├── env.py
│   │       ├── script.py.mako
│   │       └── versions/
│   │           └── 0001_initial.py
│   ├── scraper/
│   │   ├── article_data.py    # Pydantic carrier
│   │   ├── fetcher.py         # Crawl4AI wrapper + stealth
│   │   ├── rate_limiter.py    # token bucket + CB + backoff
│   │   ├── listing_crawler.py # Stage A
│   │   ├── article_crawler.py # Stage B + DB upsert
│   │   ├── pipeline.py        # tiered extraction
│   │   ├── scheduler.py       # APScheduler
│   │   ├── extractors/
│   │   │   ├── base.py
│   │   │   ├── rss_extractor.py
│   │   │   ├── jsonld_extractor.py
│   │   │   ├── crawl4ai_schema_extractor.py
│   │   │   ├── opengraph_extractor.py
│   │   │   ├── trafilatura_extractor.py
│   │   │   ├── newspaper_extractor.py
│   │   │   ├── css_fallback_extractor.py
│   │   │   └── llm_extractor.py            # feature-flagged
│   │   ├── config/
│   │   │   ├── selectors.yaml              # edit this when DOM changes
│   │   │   └── crawl4ai_schema.json        # edit this when DOM changes
│   │   └── tools/
│   │       └── refresh_selectors.py
│   └── main.py                # entrypoint
├── tests/
│   ├── fixtures/              # saved HTML for offline tests
│   ├── unit/
│   └── integration/
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh
├── alembic.ini
├── requirements.txt
├── .env.example
└── README.md
```

---

## Quickstart

```bash
# 1. Clone & configure
cp .env.example .env
# edit .env — at minimum, set DB_PASSWORD to something real

# 2. Build & run (this runs migrations automatically and starts the scheduler)
docker compose up -d --build

# 3. Tail logs
docker compose logs -f scraper-app

# 4. Inspect the database
#    pgAdmin is on http://localhost:5050 (admin@aiscraper.local / admin)
#    Or: docker compose exec postgres-db psql -U aiscraper -d aiscraper
#    And:  SELECT COUNT(*) FROM articles;
```

The `scraper-app` container:

- waits for the DB healthcheck,
- runs `alembic upgrade head`,
- starts APScheduler, which runs Stage A + Stage B immediately
  (configurable) and then every `SCHEDULER_INTERVAL_MINUTES` minutes.

---

## Configuration

All knobs are in `.env` / `app/core/config.py`. The defaults are
**conservative** — favor not getting blocked over speed.

Most important:

| Var | Default | What it does |
|-----|---------|--------------|
| `REQUESTS_PER_MINUTE` | 8 | combined rate across both stages |
| `MIN_DELAY_SEC` / `MAX_DELAY_SEC` | 1.5 / 4.0 | random jitter window |
| `MAX_CONCURRENT_REQUESTS` | 2 | semaphore cap |
| `FAILURE_THRESHOLD` | 3 | 403/429 streak before cooldown |
| `FAILURE_COOLDOWN_SEC` | 300 | cool-down after a streak |
| `RESPECT_ROBOTS_TXT` | true | toggle robots.txt gate |
| `ENABLE_LLM_EXTRACTOR` | false | turn on Tier 8 |
| `SCHEDULER_INTERVAL_MINUTES` | 30 | run cadence |
| `RECHECK_WINDOW_HOURS` | 12 | re-scrape articles older than this |
| `FORCE_REFRESH` | false | bypass dedupe, re-scrape everything |

---

## Updating selectors when Yahoo changes

This is the **only** routine maintenance the system needs. The
extraction tiers that depend on Yahoo's DOM (Tiers 3 and 7) read
exclusively from these two files:

- `app/scraper/config/selectors.yaml` — used by `CssFallbackExtractor`
  and as a structural map of the listing page.
- `app/scraper/config/crawl4ai_schema.json` — used by
  `Crawl4AISchemaExtractor`.

When extraction suddenly looks empty:

```bash
# 1. Fetch a fresh sample HTML of the live page (uses production stealth):
docker compose exec scraper-app python -m app.scraper.tools.refresh_selectors

# 2. Open the saved sample:
#    app/scraper/config/_samples/listing_sample.html
# 3. Inspect the DOM, update selectors.yaml / crawl4ai_schema.json.
# 4. Run the test suite (it uses offline fixtures and won't hit the network):
docker compose exec scraper-app pytest
# 5. Roll out:
docker compose up -d --build scraper-app
```

The tool also prints a "selector health report" — counts of matches per
common probe — so you can see at a glance which patterns broke.

**The logs will also tell you.** Every tier emits a `WARNING` with the
tag *"possible site structure change"* when its selector returns zero
matches. If you see that, refresh.

---

## Testing

```bash
# Inside the running container:
docker compose exec scraper-app pytest

# Or locally (Python 3.11+, with deps installed):
pip install -r requirements.txt
pytest
```

What's covered:

- **Unit tests per extractor tier** — `tests/unit/` runs each tier against
  offline HTML fixtures in `tests/fixtures/`.
- **Rate limiter tests** — `tests/unit/test_rate_limiter.py` simulates
  burst requests (verify throttling), and mock 403/429 responses
  (verify circuit breaker opens after `FAILURE_THRESHOLD`).
- **Integration test** — `tests/integration/test_pipeline.py` runs the
  full pipeline against stored listing + article HTML fixtures end-to-end.

No live Yahoo dependency in CI — fixtures are committed in `tests/fixtures/`.

---

## Observability

- **Structured logs** (loguru). Set `LOG_JSON=true` for JSON output suitable
  for ELK / Loki / CloudWatch.
- Per-stage logs: fetch start, rate-limit wait, extract, validate, save,
  backoff, cooldown event.
- **Metrics in `scrape_runs`**: pages fetched, new / duplicate / failed
  article counts, rate-limit hits, cooldown events, full error list.
- A summary line is logged at the end of every run:
  `=== Run <uuid> end: pages=N new=N dup=N fail=N cooldowns=N ===`.

---

## Limitations & honest caveats

- **Yahoo Finance bot protection is real and evolves.** The defaults are
  conservative, but there is no guarantee you will never see a 403.
  If you need higher reliability, plug a residential proxy rotator into
  `Fetcher.set_proxy_provider(...)` — the interface is already there.
- **Current best-effort selectors** are in `selectors.yaml` /
  `crawl4ai_schema.json`. They reflect common patterns observed in
  recent Yahoo DOMs. If a release breaks them, the healthcheck tool
  and the `WARNING` logs will tell you, and the fix is a YAML/JSON
  edit — no Python changes.
- **RSS for `/topic/stock-market-news/`** is not currently exposed by
  Yahoo. Tier 1 is wired in but is a no-op today. The moment a feed
  URL appears, the pipeline picks it up automatically.
- **LLM extractor** is off by default to avoid surprise cost. Enable
  it only after reading the prompt construction in
  `app/scraper/extractors/llm_extractor.py`.
- **First-time Playwright browser install** can take a few minutes during
  the Docker build. Subsequent builds are cached.
