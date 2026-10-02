# SEO HTML Parser & Audit Service

Deterministic FastAPI service: receives raw rendered HTML from a workflow's Web Scraping node,
parses it with BeautifulSoup + lxml, and returns structured SEO data plus a rule-based audit.
No LLM, no network calls, no JavaScript execution. Identical input always yields identical output.

## Workflow integration

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
  main.py                  FastAPI app, error handlers, body-size limit
  config.py                env-driven thresholds/limits
  api/seo.py               /api/seo/parse, /api/seo/audit (audit reuses parse_html)
  models/request.py        input validation (absolute http(s) url, non-empty html, size)
  models/response.py       Pydantic output models
  services/html_parser.py  HTML → ParsedPage (single parse pass)
  services/content_cleaner.py  noise stripping, main-content selection, word counting
  services/url_utils.py    resolve/normalize/classify URLs, private-IP detection
  services/seo_auditor.py  deterministic checks + scoring
  rules/seo_rules.py       rule catalogue (category, severity, recommendation), penalties, grades
  tests/                   pytest suite + HTML fixtures
```

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

Docker:

```bash
docker build -t seo-parser .
docker run -p 8000:8000 --env-file .env.example seo-parser
```

Configuration (env vars, see `.env.example`): `SEO_MAX_HTML_BYTES` (default 10 MB),
`SEO_TITLE_MIN/MAX` (30/60), `SEO_META_DESC_MIN/MAX` (70/160), `SEO_VERY_THIN_WORDS` (100),
`SEO_THIN_WORDS` (300).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` |
| POST | `/api/seo/parse` | Full structured extraction |
| POST | `/api/seo/audit` | Page summary + audit |
| POST | `/api/seo/report` | Audit + `report_markdown` (human-readable report). `?format=markdown` returns raw `text/markdown` |

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
| INVALID_LINK | warning | empty/malformed/unsupported-scheme href. Links are **not** HTTP-checked, so nothing is called "broken" |
| NOINDEX_PAGE / NOFOLLOW_PAGE | warning / info | noindex can be intentional |
| OG_TITLE / OG_DESCRIPTION / OG_IMAGE _MISSING | info | |
| TWITTER_CARD_MISSING | info | optional |
| EMPTY_MAIN_CONTENT / VERY_THIN_CONTENT / THIN_CONTENT | critical / warning / info | 0 / < 100 / < 300 words |
| STRUCTURED_DATA_NOT_FOUND | info | no valid JSON-LD |
| STRUCTURED_DATA_INVALID_JSON | warning | malformed blocks never fail the parse |

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
- The service never runs JavaScript, so content injected client-side must already be in the rendered HTML.
- No HTTP checks (links, canonical targets, robots.txt, X-Robots-Tag headers).
- Only the first canonical tag is used; multiple canonicals are not flagged.
- Only JSON-LD is detected, not Microdata or RDFa, and schema.org vocabulary is not validated.
- Internal/external classification doesn't use the Public Suffix List, so subdomains count as external.
- Text extraction inserts spaces at element boundaries; a Thai word split across inline tags gets a space.
