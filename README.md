# SEO Audit Service

FastAPI service for SEO analysis at two levels:

- **Single page**: send HTML (`/parse`, `/audit`, `/report`) or just a URL (`/extract`) and get
  structured SEO data plus a deterministic, rule-based audit (the on-page linter).
- **Whole site**: start a crawl (`POST /api/seo/crawls`). It follows internal links and sitemaps,
  renders JavaScript pages only when needed, and reports indexability, sitemap problems and the
  internal-link architecture.

No LLM is used. The on-page audit is deterministic: identical HTML always yields identical output.

## Workflow integration

Easiest: let the service fetch the page itself.

```
HTTP POST /api/seo/extract   {"url": "<page url>"}
  → server fetches (and renders JS if needed) → page + audit + crawl info
```

Or, if your workflow already scraped the HTML:

```
Web Scraping Node
  → Raw HTML
  → HTTP POST /api/seo/audit   {"url": "<page url>", "html": "<raw html>"}
  → Structured JSON (page + audit)
  → downstream workflow
```

Send `Content-Type: application/json` and map the scraper's URL/HTML outputs to `url` / `html`.
Branch on `data.audit.critical_count` or iterate `data.audit.issues`.

## Architecture

```
app/
  main.py                      FastAPI app, error handlers, body-size limit
  config.py                    env-driven thresholds/limits
  api/seo.py                   single-page endpoints: parse, audit, report, extract
  api/crawls.py                site crawl endpoints
  models/request.py            input validation (SeoRequest, UrlRequest, CrawlOptions)
  models/response.py           Pydantic output models
  services/html_parser.py      HTML → ParsedPage (single parse pass)
  services/content_cleaner.py  noise stripping, main-content selection, word counting
  services/url_utils.py        resolve/normalize/classify URLs, crawl de-duplication keys
  services/seo_auditor.py      on-page linter: deterministic checks + scoring
  services/fetcher.py          two-stage fetch: HTTP, then Playwright only when needed
  services/crawler.py          site crawler (async BFS, robots.txt, sitemaps)
  services/sitemap.py          robots.txt + XML sitemap / sitemap index parsing
  services/indexability.py     per-page indexability states + sitemap/indexability issues
  services/link_graph.py       internal link graph: authority, orphans, depth, broken links
  services/store.py            SQLite schema, migrations, helpers
  rules/seo_rules.py           rule catalogues (page + site), penalties, grades
  tests/                       pytest suite + HTML fixtures
```

## Run

```bash
pip install -r requirements.txt
playwright install chromium        # browser used for JavaScript rendering
uvicorn app.main:app --reload --port 8000
pytest -q
```

Windows cmd: put the command on one line and escape the inner quotes:

```
curl -X POST http://localhost:8000/api/seo/extract -H "Content-Type: application/json" -d "{\"url\": \"https://example.com\"}"
```

Docker (the image now includes Chromium, so it is several hundred MB larger):

```bash
docker build -t seo-parser .
docker run -p 8000:8000 -v seo-data:/srv/data --env-file .env.example seo-parser
```

Mount `/srv/data` to keep crawl results across container restarts.

### Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `SEO_MAX_HTML_BYTES` | 10 MB | max HTML size (request body or fetched page) |
| `SEO_TITLE_MIN` / `MAX` | 30 / 60 | title length limits |
| `SEO_META_DESC_MIN` / `MAX` | 70 / 160 | meta description length limits |
| `SEO_VERY_THIN_WORDS` / `SEO_THIN_WORDS` | 100 / 300 | thin-content thresholds |
| `SEO_RENDER_MIN_WORDS` | 50 | below this many words the raw HTML is treated as a JS shell |
| `SEO_RENDER_CONCURRENCY` | 2 | max simultaneous headless browsers |
| `SEO_DEEP_PAGE_DEPTH` | 4 | click depth at which a page counts as "deep" |
| `SEO_DB_PATH` | `data/seo.db` | SQLite file for crawl results |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` |
| POST | `/api/seo/parse` | Full structured extraction from supplied HTML |
| POST | `/api/seo/audit` | Page summary + audit from supplied HTML |
| POST | `/api/seo/report` | Audit + `report_markdown` (human-readable report). `?format=markdown` returns raw `text/markdown` |
| POST | `/api/seo/extract` | `{"url": ...}` only: the server fetches the page, renders JS if needed, and returns page + audit + `crawl` info. `?render=auto`, `never` or `always` |
| POST | `/api/seo/crawls` | Start a site crawl. Returns `crawl_id`; the crawl runs in the background |
| GET | `/api/seo/crawls/{id}` | Crawl status, options and diagnostics (`stats`) |
| GET | `/api/seo/crawls/{id}/pages` | Crawled pages. Filters: `crawl_status`, `indexable=true/false`, `limit`, `offset`, `include_audit=true` |
| GET | `/api/seo/crawls/{id}/links` | Link edges. Filter: `internal=true/false` |
| GET | `/api/seo/crawls/{id}/issues` | Site-level sitemap, indexability and architecture issues, plus indexability state counts |
| GET | `/api/seo/crawls/{id}/architecture` | Link-graph summary, top pages by authority, flagged pages |

`/parse`, `/audit` and `/report` responses are unchanged from earlier versions.

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/api/seo/audit -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/page","html":"<html><head><title>Hi</title></head><body><h1>Hi</h1></body></html>"}'
```

### Audit response (abridged, AI UTCC-style fixture)

```json
{
  "success": true,
  "data": {
    "page": {"url": "https://ai.utcc.ac.th/th", "domain": "ai.utcc.ac.th", "title": "AI UTCC",
             "title_length": 7, "canonical": "http://10.7.45.121/th", "language": "th",
             "h1": ["AI Integrated University"], "images_total": 3, "images_without_alt": 1,
             "internal_links_count": 3, "external_links_count": 1,
             "word_count": 54, "main_content_word_count": 31, "...": "..."},
    "audit": {
      "score": 35, "grade": "poor", "critical_count": 2, "warning_count": 4,
      "info_count": 4, "passed_count": 3,
      "issues": [{"code": "CANONICAL_PRIVATE_IP", "category": "canonical", "severity": "critical",
                  "message": "Canonical points to private/internal host 10.7.45.121.",
                  "value": "http://10.7.45.121/th", "count": 1,
                  "recommendation": "The canonical points to a private/internal host; use the public URL."}]
    }
  }
}
```

## Render-aware fetching (`/extract` and crawls)

1. Fetch the URL with plain HTTP and parse the raw HTML.
2. Decide whether a browser is needed. Rendering is triggered by a thin page
   (fewer than `SEO_RENDER_MIN_WORDS` words), an empty app root (`<div id="root"></div>` and similar),
   or a client-side framework marker (React, Vue, Angular, Svelte) together with no headings or few links.
   A content-rich, server-rendered React page is **not** rendered.
3. If needed, load the page in headless Chromium (Playwright) and parse the rendered DOM.
4. Use the rendered DOM only if it adds substantially more words, headings or links.

The `crawl` block reports both states: `render_required`, `render_reasons`, `render_status`
(`not_needed`, `rendered`, `failed` or `skipped`), `render_duration_ms`, `render_error`,
`source` (`raw` or `rendered`), `raw_word_count` and `rendered_word_count`.
A render failure never fails the request; the raw HTML is analysed instead.
When content only exists after rendering, the audit adds `CONTENT_REQUIRES_JS` (warning).

## Site crawls

```bash
curl -X POST localhost:8000/api/seo/crawls -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/", "max_pages": 100, "concurrency": 4}'
# → 202 {"data": {"crawl_id": 1, "status": "queued"}}
curl localhost:8000/api/seo/crawls/1          # repeat until status is completed or failed
curl localhost:8000/api/seo/crawls/1/issues
```

The POST returns immediately; poll the crawl until `status` is `completed`.

### Options

| Option | Default | Meaning |
|---|---|---|
| `url` | required | start URL |
| `max_pages` | 200 | max URLs requested (1–10,000) |
| `max_depth` | 5 | max link depth from the start URL |
| `concurrency` | 5 | parallel HTTP requests (1–20) |
| `request_timeout` | 15 | seconds per request |
| `same_domain_only` | true | `false` also crawls subdomains of the start domain |
| `respect_robots_txt` | true | skip URLs disallowed for `SeoAuditBot` or `*` |
| `follow_redirects` | true | `false` records the redirect and queues its target separately |
| `use_sitemaps` | true | read sitemaps listed in robots.txt, else `/sitemap.xml` (sitemap indexes and .gz supported) |
| `render_javascript` | true | `false` never launches a browser |
| `render_strategy` | `auto` | `auto`, `never` or `always` |
| `include_patterns` / `exclude_patterns` | `[]` | regexes matched against the URL |

### How crawling works

- URLs found through links are crawled before sitemap-only URLs, so `depth` is real click depth.
  Pages reachable only through the sitemap get `depth: null` and `discovered_via: "sitemap"`.
- De-duplication: fragments, tracking parameters (`utm_*`, `gclid`, `fbclid` and others), duplicate
  and reordered query parameters, and trailing-slash variants map to the same URL. A redirect
  (for example http → https) is recorded once, and its target is crawled once.
- External domains are never crawled; external links are recorded.
- Links with `rel=nofollow`, and links on pages with a meta `nofollow`, are not followed.
- Redirect loops are recorded as errors (`crawl_status: "error"`) and never hang the crawl.
- Every HTML page goes through the on-page linter, and its audit is stored per page.
- After crawling, the indexability engine and the link-graph analysis run. If either fails,
  the crawl is still saved.

Per-page `crawl_status`: `ok`, `redirect`, `blocked_robots`, `non_html` or `error`.
HTTP 4xx/5xx pages are `ok` with their `status_code`.

### Diagnostics (`stats` on the crawl)

`duration_ms`, `http_duration_ms`, `render_duration_ms` (summed across parallel renders),
`discovered_urls`, `requested_urls`, `crawled_ok`, `redirects`, `non_html`, `failed`,
`blocked_by_robots`, `skipped` (by depth, pattern or max_pages), `rendered_with_browser`,
`render_failed`, `render_errors`, `depth_distribution`, `sitemap_urls`, `sitemap_files`,
plus `indexability` (state counts) and `architecture` (link-graph summary). HTML is never logged.

### Indexability states

Each page gets separate states instead of one pass/fail flag. Each state has a `value`
(`true`, `false`, or `null` for unknown) and a `reason`:

| State | False when |
|---|---|
| `discovered` | never false: the page row exists |
| `crawlable` | blocked by robots.txt |
| `fetchable` | the request failed, or HTTP 4xx/5xx |
| `renderable` | browser rendering failed |
| `indexable` | not fetchable, a redirect, meta robots `noindex`/`none`, or `X-Robots-Tag` noindex (including `googlebot: noindex`) |
| `canonical` | the canonical points to another URL or a private host, or is invalid |

Plus `soft_404_candidate`: the page returns HTTP 200 but its title or H1 reads like an error page.

### Site issues (`/issues`)

| Code | Severity | Meaning |
|---|---|---|
| SITEMAP_NOT_FOUND | warning | no reachable sitemap |
| SITEMAP_EMPTY_OR_INVALID | critical | the sitemap returns 200 but has no `<loc>` URLs (for example, an SPA's HTML fallback) |
| SITEMAP_NOT_IN_ROBOTS | info | robots.txt has no `Sitemap:` line |
| SITEMAP_URL_NON_200 / _REDIRECT / _BLOCKED / _NOINDEX / _CANONICALIZED | warning | the sitemap lists URLs that error, redirect, are blocked, are noindex, or are canonicalized |
| SITEMAP_DUPLICATE_URLS | info | a URL is listed more than once |
| PAGES_MISSING_FROM_SITEMAP | warning | an indexable, self-canonical page is not in the sitemap |
| CANONICAL_TO_NON_INDEXABLE | critical | the canonical points to a private host, an error page or an invalid URL |
| CANONICALIZED_PAGES / NOINDEX_PAGES / ROBOTS_BLOCKED_PAGES | info | confirm these are intentional |
| BROKEN_PAGES | warning | discovered URLs that return errors |
| SOFT_404_CANDIDATES | warning | error-looking pages that return 200 |
| RENDER_FAILED_PAGES | warning | browser rendering failed |
| BROKEN_INTERNAL_LINKS | critical | internal links to error pages (with source, target and anchor text) |
| REDIRECTED_INTERNAL_LINKS | warning | internal links that go through a redirect |
| ORPHAN_PAGE_CANDIDATES | warning | no followed internal links from crawled pages |
| IMPORTANT_PAGES_TOO_DEEP | warning | a top-20% authority or in-sitemap page at depth ≥ `SEO_DEEP_PAGE_DEPTH` |
| DEAD_END_PAGES / WEAKLY_LINKED_PAGES / DEEP_PAGES | info | no outgoing links / only one linking page / deep |

Each issue has a `count` and up to 20 examples in `value`.

### Internal link graph (`/architecture`)

- Every internal link is stored with its anchor text, nofollow flag and `location`
  (`nav`, `header`, `footer`, `aside`, `main` or `body`: the nearest semantic container).
- `authority` (0–100) is a simplified PageRank over followed links between live pages.
  Redirects pass through to their target. Use it for relative importance, not as an absolute metric.
- Per-page `link_metrics`: `authority`, `unique_inlinks`, `unique_outlinks` and `flags`
  (`orphan_candidate`, `weak_internal_linking`, `dead_end`, `deep`, `important_but_deep`).

### Database

SQLite at `SEO_DB_PATH`, in WAL mode so the API can read while a crawl writes. Tables:
`crawls`, `crawl_pages`, `page_links`. New columns are added automatically on startup
(see `MIGRATIONS` in `services/store.py`), so existing databases keep working.

### Errors

`{"success": false, "error": {"code": "...", "message": "...", "details": [...]}}`.
`422 VALIDATION_ERROR` (missing/invalid url or html, bad JSON), `413 PAYLOAD_TOO_LARGE`,
`500 INTERNAL_ERROR` (no stack trace; logged server-side). Malformed HTML is parsed leniently.

## Parser notes

- **Links**: each `<a href>` gets `type` = internal | external | anchor | mailto | tel | javascript | invalid.
  Internal = same hostname, ignoring a leading `www.` (subdomains count as external). `<base href>` is honoured.
- **Duplicates**: a link is `is_duplicate: true` if an earlier link had the same (type, normalized URL, text).
  Normalization lowercases scheme/host, drops default ports, fragments and trailing slashes.
  `links` keeps every raw link; `links_total` and the per-type counts use unique links only.
- **Images**: `alt: null` = attribute missing; `alt: ""` = decorative. Counted separately.
- **Main content**: `<main>`/`[role=main]` → a single `<article>` → `<body>`. `nav` is always removed;
  `header`/`footer` are removed only in the body fallback, so article bylines stay.
  script/style/svg/canvas/noscript/template/iframe/object are stripped before text extraction.

## Audit rules

| Code | Severity | Notes |
|---|---|---|
| TITLE_MISSING / TOO_SHORT / TOO_LONG | critical / warning / warning | < 30 / > 60 chars |
| META_DESCRIPTION_MISSING / TOO_SHORT / TOO_LONG | warning / info / info | < 70 / > 160 chars |
| H1_MISSING, MULTIPLE_H1, EMPTY_HEADING | warning | |
| HEADING_ORDER_SKIPPED | info | descending more than one level (H1→H3); going back up is fine |
| CANONICAL_MISSING | warning | |
| CANONICAL_INVALID | critical | empty / non-http(s) / malformed |
| CANONICAL_PRIVATE_IP | critical | private, loopback, link-local, reserved IPs, localhost |
| CANONICAL_DOMAIN_MISMATCH | warning | skipped when already PRIVATE_IP |
| CANONICAL_HTTP_WHEN_PAGE_HTTPS | warning | |
| HREFLANG_INVALID | warning | bad lang code (`ll[-Ssss][-RR]`, x-default) or bad URL |
| HREFLANG_PRIVATE_IP | critical | |
| HREFLANG_DOMAIN_MISMATCH | info | cross-domain hreflang can be legitimate |
| HREFLANG_DUPLICATE_LANGUAGE | warning | case-insensitive |
| IMAGE_ALT_MISSING | warning | alt attribute absent; `alt=""` is not flagged |
| EMPTY_LINK | warning | no text, aria-label, title or img alt |
| JAVASCRIPT_LINK | info | |
| INVALID_LINK | warning | empty/malformed/unsupported-scheme href. The single-page audit does not HTTP-check links; crawls report broken links as `BROKEN_INTERNAL_LINKS` |
| NOINDEX_PAGE / NOFOLLOW_PAGE | warning / info | noindex can be intentional |
| OG_TITLE / OG_DESCRIPTION / OG_IMAGE _MISSING | info | |
| TWITTER_CARD_MISSING | info | optional |
| EMPTY_MAIN_CONTENT / VERY_THIN_CONTENT / THIN_CONTENT | critical / warning / info | 0 / < 100 / < 300 words |
| STRUCTURED_DATA_NOT_FOUND | info | no valid JSON-LD |
| STRUCTURED_DATA_INVALID_JSON | warning | malformed blocks never fail the parse |
| CONTENT_REQUIRES_JS | warning | `/extract` and crawls only: the raw HTML is nearly empty and content appears only after JS rendering |

Each check group with no issues emits a `passed` entry (`<GROUP>_OK`). Issues are aggregated
per code (`count`, up to 10 examples in `value`) and sorted by severity, category, code.

## Score calculation

```
penalty(code) = min(BASE[severity] × count, CAP[severity])
BASE: critical 15, warning 5, info 0      CAP: critical 30, warning 10
score = clamp(100 − Σ penalty, 0, 100)
grade: ≥90 excellent, ≥75 good, ≥50 needs_improvement, else poor
```

The cap means 50 images without alt cost 10 points, not 250. All constants are in `app/rules/seo_rules.py`.

## Known limitations

- **Thai word counts are approximate.** Thai has no spaces between words, so each Thai run counts as
  `ceil(chars / 5)` words. `word_count_is_approximate` is true when Thai text is present.
  For exact counts, add a dictionary segmenter such as `pythainlp`.
- `/parse`, `/audit` and `/report` never run JavaScript or make HTTP requests: they analyse the HTML
  you send. Use `/extract` or a crawl for fetching, rendering and HTTP-level checks.
- Each browser render launches a fresh Chromium (about 2–4 s per page), so large JS-heavy crawls are slow.
  Lower `max_pages` or use `render_strategy: "never"` for a quick pass.
- Crawls run inside the API process as FastAPI background tasks. Restarting the server stops a running
  crawl, and it stays `running` in the database.
- Soft-404 detection only looks at title and H1 wording (English and Thai).
- Only the first canonical tag is used; multiple canonicals are not flagged.
- Only JSON-LD is detected, not Microdata or RDFa, and schema.org vocabulary is not validated.
- Internal/external classification doesn't use the Public Suffix List, so subdomains count as external.
- Text extraction inserts spaces at element boundaries; a Thai word split across inline tags gets a space.
