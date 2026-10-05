"""Two-stage page fetch: plain HTTP first, Playwright only when the raw HTML looks client-rendered."""
import re
import threading
import time

import httpx
from pydantic import BaseModel

from app.config import settings
from app.models.response import CrawlInfo, ParsedPage, RenderMode
from app.services.html_parser import parse_html

USER_AGENT = "Mozilla/5.0 (compatible; SeoAuditBot/1.0)"

_FRAMEWORK_MARKERS = {
    "react": re.compile(r"data-reactroot|__NEXT_DATA__|react(-dom)?(\.production)?(\.min)?\.js", re.I),
    "vue": re.compile(r"data-v-app|__NUXT__|vue(\.runtime)?(\.global)?(\.prod)?(\.min)?\.js", re.I),
    "angular": re.compile(r"ng-version|ng-app|<app-root", re.I),
    "svelte": re.compile(r"__sveltekit|svelte-", re.I),
}
_APP_ROOT = re.compile(r'<div[^>]+id=["\'](root|app|__next|__nuxt|svelte)["\'][^>]*>\s*</div>', re.I)
_SCRIPT = re.compile(r"<script\b", re.I)


class FetchError(Exception):
    pass


class RawFetch(BaseModel):
    status_code: int
    final_url: str
    content_type: str
    headers: dict[str, str]
    html: str
    duration_ms: int
    redirect_chain: list[str] = []  # URLs that redirected, in order (excludes final_url)


class CrawledPage(BaseModel):
    page: ParsedPage
    raw: ParsedPage
    rendered: ParsedPage | None
    info: CrawlInfo


def to_raw_fetch(url: str, resp: httpx.Response, start: float) -> RawFetch:
    if len(resp.content) > settings.max_html_bytes:
        raise FetchError(f"Response from {url} exceeds {settings.max_html_bytes} bytes")
    return RawFetch(
        status_code=resp.status_code,
        final_url=str(resp.url),
        content_type=resp.headers.get("content-type", ""),
        headers={k.lower(): v for k, v in resp.headers.items()},
        html=resp.text,
        duration_ms=int((time.perf_counter() - start) * 1000),
        redirect_chain=[str(r.url) for r in resp.history],
    )


def fetch_raw(url: str, timeout: float = 15) -> RawFetch:
    start = time.perf_counter()
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as exc:
        raise FetchError(f"Failed to fetch {url}: {exc}") from exc
    return to_raw_fetch(url, resp, start)


def render_reasons(html: str, page: ParsedPage) -> list[str]:
    """Heuristics that the server HTML is a client-side shell. Empty list = rendering not needed."""
    reasons = []
    words = page.word_count
    if words < settings.render_min_words:
        reasons.append(f"low visible word count ({words} < {settings.render_min_words})")
    if not page.heading_order:
        reasons.append("no headings")
    if page.internal_links_count + page.external_links_count < 3:
        reasons.append("fewer than 3 meaningful links")
    if _APP_ROOT.search(html):
        reasons.append("empty client-side app root element")
    frameworks = [name for name, rx in _FRAMEWORK_MARKERS.items() if rx.search(html)]
    if frameworks:
        reasons.append(f"client-side framework detected: {', '.join(frameworks)}")
    scripts = len(_SCRIPT.findall(html))
    if scripts >= 5 and words < settings.render_min_words * 2:
        reasons.append(f"{scripts} scripts with minimal HTML content")
    # Few links / no headings alone are normal on simple pages; they only count alongside a
    # framework marker. A content-rich page that merely uses React (SSR) is not rendered.
    thin = words < settings.render_min_words or _APP_ROOT.search(html)
    weak = not page.heading_order or page.internal_links_count + page.external_links_count < 3
    if not (thin or (frameworks and weak)):
        return []
    return reasons


# ponytail: one browser launch per render; reuse a browser pool if crawls render many pages.
_RENDER_SLOTS = threading.BoundedSemaphore(settings.render_concurrency)


def render_html(url: str, timeout: float = 30) -> tuple[str, int]:
    """Load the page in headless Chromium and return the rendered DOM. Sync API; call from a worker thread."""
    from playwright.sync_api import sync_playwright  # imported lazily: heavy and optional

    start = time.perf_counter()
    with _RENDER_SLOTS, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            html = page.content()
        finally:
            browser.close()
    return html, int((time.perf_counter() - start) * 1000)


def crawl_page(url: str, mode: RenderMode = "auto") -> CrawledPage:
    return analyze(fetch_raw(url), mode)


def analyze(raw_fetch: RawFetch, mode: RenderMode = "auto") -> CrawledPage:
    """Parse raw HTML, render with a browser if needed, and pick the better DOM. Blocking."""
    raw = parse_html(raw_fetch.final_url, raw_fetch.html)
    is_html = "html" in raw_fetch.content_type or not raw_fetch.content_type

    reasons = render_reasons(raw_fetch.html, raw) if is_html else []
    required = mode == "always" or (mode == "auto" and bool(reasons))
    if mode == "always":
        reasons = reasons or ["render_mode=always"]

    rendered, status, render_ms, error = None, "not_needed", None, None
    if reasons and mode == "never":
        status = "skipped"
    elif required and is_html and raw_fetch.status_code < 400:
        try:
            html, render_ms = render_html(raw_fetch.final_url)
            rendered = parse_html(raw_fetch.final_url, html)
            status = "rendered"
        except Exception as exc:  # any browser failure falls back to raw analysis
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]

    # Use the rendered DOM only when it adds substantially more content.
    use_rendered = rendered is not None and (
        rendered.word_count >= raw.word_count * 1.2 + 20
        or len(rendered.heading_order) > len(raw.heading_order)
        or rendered.links_total > raw.links_total + 5
    )
    info = CrawlInfo(
        status_code=raw_fetch.status_code,
        final_url=raw_fetch.final_url,
        content_type=raw_fetch.content_type,
        x_robots_tag=raw_fetch.headers.get("x-robots-tag"),
        http_duration_ms=raw_fetch.duration_ms,
        render_mode=mode,
        render_required=required,
        render_reasons=reasons,
        render_status=status,
        render_duration_ms=render_ms,
        render_error=error,
        source="rendered" if use_rendered else "raw",
        raw_word_count=raw.word_count,
        rendered_word_count=rendered.word_count if rendered else None,
    )
    return CrawledPage(page=rendered if use_rendered else raw, raw=raw, rendered=rendered, info=info)
