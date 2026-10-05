"""Site crawler: BFS over internal links + sitemap URLs, render-aware page analysis, results to SQLite.
Link-discovered URLs are always crawled before sitemap-only URLs so depth reflects click depth."""
import asyncio
import logging
import re
import time
from collections import Counter, deque
from urllib.parse import urljoin

import httpx

from app.models.request import CrawlOptions
from app.services import indexability, store
from app.services.fetcher import USER_AGENT, analyze, to_raw_fetch
from app.services.seo_auditor import audit_page
from app.services.sitemap import collect_sitemap_urls, fetch_robots
from app.services.url_utils import crawl_key, host_of, is_web_url, site_key

log = logging.getLogger("seo.crawler")
ROBOTS_TOKEN = "SeoAuditBot"
_MAX_RENDER_ERRORS = 20


class _Crawl:
    def __init__(self, crawl_id: int, opts: CrawlOptions, conn, transport=None):
        self.id, self.opts, self.conn, self.transport = crawl_id, opts, conn, transport
        self.root = site_key(host_of(opts.url))
        self.include = [re.compile(p) for p in opts.include_patterns]
        self.exclude = [re.compile(p) for p in opts.exclude_patterns]
        self.mode = opts.render_strategy if opts.render_javascript else "never"
        self.seen: set[str] = set()          # crawl keys queued or recorded
        self.sitemap_keys: set[str] = set()
        self.link_q: deque = deque()         # (url, depth, parent_url, via)
        self.sitemap_q: deque = deque()
        self.fetched = 0
        self.robots = None
        self.stats = Counter()
        self.timing = Counter()
        self.depths = Counter()
        self.render_errors: list[dict] = []

    # --- scope -------------------------------------------------------------
    def in_domain(self, url: str) -> bool:
        if not is_web_url(url):
            return False
        host = site_key(host_of(url))
        return host == self.root or (not self.opts.same_domain_only and host.endswith("." + self.root))

    def allowed_by_patterns(self, url: str) -> bool:
        if self.include and not any(p.search(url) for p in self.include):
            return False
        return not any(p.search(url) for p in self.exclude)

    def enqueue(self, url: str, depth: int | None, parent: str | None, via: str) -> None:
        key = crawl_key(url)
        if key in self.seen:
            return
        if not self.allowed_by_patterns(url):
            self.stats["skipped_pattern"] += 1
            return
        if depth is not None and depth > self.opts.max_depth:
            self.stats["skipped_depth"] += 1
            return
        if via == "sitemap":  # not marked seen yet, so a later link discovery still sets real depth
            self.sitemap_q.append((url, depth, parent, via))
            return
        self.seen.add(key)
        self.link_q.append((url, depth, parent, via))

    # --- persistence -------------------------------------------------------
    def record(self, url: str, key: str, depth, parent, via, crawl_status: str, **fields) -> None:
        row = {"crawl_id": self.id, "url": url, "normalized_url": key, "crawl_status": crawl_status,
               "depth": depth, "parent_url": parent, "discovered_via": via,
               "in_sitemap": int(key in self.sitemap_keys), "crawled_at": store.now(), **fields}
        store.insert(self.conn, "crawl_pages", row)
        self.conn.commit()
        self.stats[f"status_{crawl_status}"] += 1
        if crawl_status == "ok":
            self.depths["sitemap_only" if depth is None else str(depth)] += 1

    # --- work --------------------------------------------------------------
    async def process(self, client: httpx.AsyncClient, url: str, depth, parent, via) -> None:
        key = crawl_key(url)
        start = time.perf_counter()
        try:
            resp = await client.get(url)
            raw = to_raw_fetch(url, resp, start)
        except httpx.TooManyRedirects:
            self.record(url, key, depth, parent, via, "error", error="redirect loop or too many redirects")
            return
        except Exception as exc:  # network errors, oversize responses: record and move on
            self.record(url, key, depth, parent, via, "error", error=f"{type(exc).__name__}: {exc}"[:500])
            return
        self.timing["http_ms"] += raw.duration_ms

        if 300 <= raw.status_code < 400:  # only reachable with follow_redirects=False
            target = urljoin(url, raw.headers.get("location", ""))
            self.record(url, key, depth, parent, via, "redirect", status_code=raw.status_code,
                        redirect_target=target, crawl_time_ms=raw.duration_ms)
            if self.in_domain(target):
                self.enqueue(target, depth, url, via)
            return

        if raw.redirect_chain:
            final_key = crawl_key(raw.final_url)
            if final_key != key:
                status = resp.history[0].status_code
                self.record(url, key, depth, parent, via, "redirect", status_code=status,
                            redirect_target=raw.final_url, redirect_chain=raw.redirect_chain,
                            crawl_time_ms=raw.duration_ms)
                if final_key in self.seen or not self.in_domain(raw.final_url):
                    return  # target already crawled/queued, or off-site: the redirect row is enough
                self.seen.add(final_key)
                url, key, parent = raw.final_url, final_key, url

        content_type = raw.content_type
        common = dict(status_code=raw.status_code, content_type=content_type,
                      redirect_chain=raw.redirect_chain or None)
        if content_type and "html" not in content_type:
            self.record(url, key, depth, parent, via, "non_html", crawl_time_ms=raw.duration_ms, **common)
            return

        crawled = await asyncio.to_thread(analyze, raw, self.mode)
        info, page = crawled.info, crawled.page
        if info.render_duration_ms:
            self.timing["render_ms"] += info.render_duration_ms
        if info.render_status == "rendered":
            self.stats["rendered"] += 1
        elif info.render_status == "failed":
            self.stats["render_failed"] += 1
            if len(self.render_errors) < _MAX_RENDER_ERRORS:
                self.render_errors.append({"url": url, "error": info.render_error})

        internal_out = external_out = 0
        links = []
        follow_links = not page.nofollow and raw.status_code < 400
        for link in page.links:
            if link.is_duplicate or link.type not in ("internal", "external") or not link.absolute_url:
                continue
            internal = self.in_domain(link.absolute_url)
            target = crawl_key(link.absolute_url) if internal else link.absolute_url
            links.append((self.id, key, target, link.text[:500], int(internal), int(link.nofollow)))
            if internal:
                internal_out += 1
                if follow_links and not link.nofollow:
                    self.enqueue(link.absolute_url, None if depth is None else depth + 1, url, "link")
            else:
                external_out += 1
        self.conn.executemany("INSERT INTO page_links (crawl_id, source_url, target_url, anchor_text, "
                              "internal, nofollow) VALUES (?, ?, ?, ?, ?, ?)", links)

        self.record(
            url, key, depth, parent, via, "ok", **common,
            canonical_url=page.canonical_absolute_url,
            robots_directives={"meta": page.meta_robots, "x_robots_tag": info.x_robots_tag},
            title=page.title, meta_description=page.meta_description,
            h1=page.h1[0] if page.h1 else None, word_count=page.word_count, language=page.language,
            crawl_time_ms=raw.duration_ms + (info.render_duration_ms or 0),
            render_required=int(info.render_required), render_status=info.render_status,
            render_info=info.model_dump(), internal_links_out=internal_out,
            external_links_out=external_out, audit=audit_page(page, crawl=info).model_dump(),
        )

    def check_robots(self, url: str, depth, parent, via) -> bool:
        if self.opts.respect_robots_txt and self.robots and not self.robots.can_fetch(ROBOTS_TOKEN, url):
            self.record(url, crawl_key(url), depth, parent, via, "blocked_robots",
                        error="blocked by robots.txt")
            return False
        return True

    async def run(self) -> None:
        opts = self.opts
        async with httpx.AsyncClient(timeout=opts.request_timeout, follow_redirects=opts.follow_redirects,
                                     headers={"User-Agent": USER_AGENT}, transport=self.transport) as client:
            self.robots, robots_sitemaps, robots_status = await fetch_robots(client, opts.url)
            sitemap_found: list[str] = []
            sitemap_files: list[dict] = []
            if opts.use_sitemaps:
                sources = robots_sitemaps or [urljoin(opts.url, "/sitemap.xml")]
                sitemap_found, sitemap_files = await collect_sitemap_urls(client, sources, opts.max_pages * 5)
            self.sitemap_keys = {crawl_key(u) for u in sitemap_found if is_web_url(u)}
            store.update(self.conn, "crawls", self.id, {
                "robots": {"status": robots_status, "sitemaps": robots_sitemaps},
                "sitemap_urls": sitemap_found})
            self.conn.commit()

            self.enqueue(opts.url, 0, None, "root")
            for u in sitemap_found:
                if self.in_domain(u):
                    self.enqueue(u, None, None, "sitemap")

            in_flight: dict[asyncio.Task, str] = {}  # task -> via
            while self.link_q or self.sitemap_q or in_flight:
                while len(in_flight) < opts.concurrency and self.fetched < opts.max_pages:
                    # Sitemap-only URLs wait until link discovery is fully drained.
                    if self.link_q:
                        item = self.link_q.popleft()
                    elif self.sitemap_q and "link" not in in_flight.values()                             and "root" not in in_flight.values():
                        item = self.sitemap_q.popleft()
                        if crawl_key(item[0]) in self.seen:
                            continue
                        self.seen.add(crawl_key(item[0]))
                    else:
                        break
                    url, depth, parent, via = item
                    if not self.check_robots(url, depth, parent, via):
                        continue
                    self.fetched += 1
                    in_flight[asyncio.create_task(self.process(client, url, depth, parent, via))] = via
                if not in_flight:
                    break
                done, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    del in_flight[task]
                    task.result()
            self.stats["not_crawled_max_pages"] = len(self.link_q) + sum(
                crawl_key(u) not in self.seen for u, *_ in self.sitemap_q)
            self.timing["sitemap_files"] = len(sitemap_files)
            self._sitemap_files = sitemap_files

    def finalize(self, started: float) -> dict:
        self.conn.execute(
            "UPDATE crawl_pages SET internal_links_in = (SELECT COUNT(*) FROM page_links l WHERE "
            "l.crawl_id = crawl_pages.crawl_id AND l.internal = 1 AND l.target_url = crawl_pages.normalized_url "
            "AND l.source_url != l.target_url) WHERE crawl_id = ?", (self.id,))
        s = self.stats
        return {
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "http_duration_ms": self.timing["http_ms"],
            "render_duration_ms": self.timing["render_ms"],
            "discovered_urls": len(self.seen),
            "requested_urls": self.fetched,
            "crawled_ok": s["status_ok"],
            "redirects": s["status_redirect"],
            "non_html": s["status_non_html"],
            "failed": s["status_error"],
            "blocked_by_robots": s["status_blocked_robots"],
            "skipped": {"max_depth": s["skipped_depth"], "pattern": s["skipped_pattern"],
                        "max_pages": s["not_crawled_max_pages"]},
            "rendered_with_browser": s["rendered"],
            "render_failed": s["render_failed"],
            "render_errors": self.render_errors,
            "depth_distribution": dict(sorted(self.depths.items())),
            "sitemap_urls": len(self.sitemap_keys),
            "sitemap_files": getattr(self, "_sitemap_files", []),
        }


async def run_crawl(crawl_id: int, opts: CrawlOptions, conn=None, transport=None) -> None:
    """Run a crawl to completion. Never raises: failures are stored on the crawl row."""
    conn = conn or store.connect()
    crawl = _Crawl(crawl_id, opts, conn, transport)
    started = time.perf_counter()
    store.update(conn, "crawls", crawl_id, {"status": "running", "started_at": store.now()})
    conn.commit()
    try:
        await crawl.run()
        status, error = "completed", None
    except Exception as exc:
        log.exception("Crawl %s failed", crawl_id)
        status, error = "failed", f"{type(exc).__name__}: {exc}"[:1000]
    stats = crawl.finalize(started)
    store.update(conn, "crawls", crawl_id, {"stats": stats})
    try:
        indexability.apply(conn, crawl_id)
    except Exception:  # analysis failure must not lose the crawl itself
        log.exception("Indexability analysis failed for crawl %s", crawl_id)
    store.update(conn, "crawls", crawl_id, {"status": status, "error": error, "finished_at": store.now()})
    conn.commit()
    log.info("Crawl %s %s: %s ok, %s failed, %s blocked, %sms", crawl_id, status, stats["crawled_ok"],
             stats["failed"], stats["blocked_by_robots"], stats["duration_ms"])


def create_crawl(conn, opts: CrawlOptions) -> int:
    crawl_id = store.insert(conn, "crawls", {
        "root_url": opts.url, "domain": host_of(opts.url), "status": "queued",
        "options": opts.model_dump(), "created_at": store.now()})
    conn.commit()
    return crawl_id
