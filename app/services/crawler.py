"""Site crawler: BFS over internal links + sitemap URLs, render-aware page analysis, results to SQLite.
Link-discovered URLs are always crawled before sitemap-only URLs so depth reflects click depth."""
import asyncio
import logging
import re
import time
from collections import Counter, deque
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.config import settings
from app.models.request import CrawlOptions
from app.services import indexability, link_graph, page_audit_summary, store
from app.services.fetcher import USER_AGENT, analyze, to_raw_fetch
from app.services.seo_auditor import audit_page
from app.services.sitemap import collect_sitemap_urls, fetch_robots
from app.services.url_utils import (crawl_key, host_of, is_private_host, is_web_url,
                                    language_segment, resource_type, site_key)

log = logging.getLogger("seo.crawler")
ROBOTS_TOKEN = "SeoAuditBot"
_MAX_RENDER_ERRORS = 20
_SITEMAP_URL = re.compile(r"\.(xml|rss)(\.gz)?$", re.I)  # not /sitemap/ HTML pages


class _Crawl:
    def __init__(self, crawl_id: int, opts: CrawlOptions, conn, transport=None):
        self.id, self.opts, self.conn, self.transport = crawl_id, opts, conn, transport
        self.root = site_key(host_of(opts.url))
        # A sitemap given as the start URL is read as a sitemap; link discovery starts at the homepage so
        # click depth stays meaningful.
        self.start_is_sitemap = bool(_SITEMAP_URL.search(urlsplit(opts.url).path))
        self.start_url = urljoin(opts.url, "/") if self.start_is_sitemap else opts.url
        self.include = [re.compile(p) for p in opts.include_patterns]
        self.exclude = [re.compile(p) for p in opts.exclude_patterns]
        self.mode = opts.render_strategy if opts.render_javascript else "never"
        self.seen: set[str] = set()          # crawl keys queued or recorded
        self.sitemap_keys: set[str] = set()
        self.link_keys: set[str] = set()      # discovered via the start URL, links or redirects
        self.alternates: dict[str, set[str]] = {}  # hreflang lang -> alternate URLs seen on crawled pages
        self.link_q: deque = deque()         # (url, depth, parent_url, via)
        self.sitemap_q: deque = deque()
        self.fetched = 0
        self.robots = None
        self.stats = Counter()
        self.timing = Counter()
        self.depths = Counter()
        self.render_errors: list[dict] = []
        self.sitemap_meta: dict | None = None  # None = sitemaps not used

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
        if via != "sitemap":
            self.link_keys.add(key)
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
        kind = resource_type(url, fields.get("content_type"), fields.get("status_code"))
        if crawl_status == "ok" and (fields.get("status_code") or 0) < 400 and kind == "document":
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
                self.link_keys.add(final_key)
                url, key, parent = raw.final_url, final_key, url

        content_type = raw.content_type
        common = dict(status_code=raw.status_code, content_type=content_type,
                      redirect_chain=raw.redirect_chain or None)
        if content_type and "html" not in content_type:
            self.record(url, key, depth, parent, via, "non_html", crawl_time_ms=raw.duration_ms, **common)
            return

        crawled = await asyncio.to_thread(analyze, raw, self.mode)
        info, page = crawled.info, crawled.page
        if raw.status_code < 400:
            for h in page.hreflang:
                if h.absolute_url:  # group by base language: th-TH and th are the same /th version
                    lang = h.lang.lower() if h.lang.lower() == "x-default" else h.lang.lower().split("-")[0]
                    self.alternates.setdefault(lang, set()).add(h.absolute_url)
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
            links.append((self.id, key, target, link.text[:500], int(internal), int(link.nofollow),
                          link.location))
            if internal:
                internal_out += 1
                if follow_links and not link.nofollow:
                    self.enqueue(link.absolute_url, None if depth is None else depth + 1, url, "link")
            else:
                external_out += 1
        self.conn.executemany("INSERT INTO page_links (crawl_id, source_url, target_url, anchor_text, "
                              "internal, nofollow, location) VALUES (?, ?, ?, ?, ?, ?, ?)", links)

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
            self.robots_status = robots_status
            sitemap_found: list[str] = []
            sitemap_files: list[dict] = []
            if opts.use_sitemaps or self.start_is_sitemap:
                sources = robots_sitemaps or [urljoin(opts.url, "/sitemap.xml")]
                if self.start_is_sitemap:
                    sources = [opts.url, *sources]
                sitemap_found, sitemap_files, self.sitemap_meta = await collect_sitemap_urls(
                    client, sources, settings.sitemap_max_urls, settings.sitemap_max_files)
            self.sitemap_keys = {crawl_key(u) for u in sitemap_found if is_web_url(u)}
            store.update(self.conn, "crawls", self.id, {
                "robots": {"status": robots_status, "sitemaps": robots_sitemaps},
                "sitemap_urls": sitemap_found})
            self.conn.commit()

            self.enqueue(self.start_url, 0, None, "root")
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

    def coverage(self, failed: bool) -> dict:
        """Discovered vs crawled. Discovered = every in-scope URL found via links, redirects or sitemaps
        (excluding include/exclude pattern skips). complete only when nothing discovered was left over."""
        pending_sitemap = {crawl_key(u) for u, *_ in self.sitemap_q} - self.seen
        discovered = len(self.seen) + len(pending_sitemap)
        remaining = len(self.link_q) + len(pending_sitemap)
        sm = self.sitemap_meta or {}
        sitemap_status = sm.get("status") or ("not_used" if self.sitemap_meta is None else None)
        limit = self.opts.max_pages
        limit_reached = self.fetched >= limit
        sitemap_ok = sitemap_status == "ok"
        if failed:
            scope, reason = "partial", "crawl_error"
        elif remaining:
            scope, reason = "partial", "max_pages_reached"
        elif self.stats["skipped_depth"]:
            scope, reason = "partial", "max_depth_reached"
        elif sitemap_status == "partial":
            scope, reason = "partial", "sitemap_incomplete"  # unread sitemap URLs may never have been discovered
        elif limit_reached:
            # Queue happened to empty exactly at the limit: no evidence that nothing else exists.
            scope, reason = "crawl_limit_reached", "max_pages_reached"
        else:
            scope, reason = "full_known_scope", None
        langs = language_scope(self.start_url, self.root, self.seen, self.alternates)
        # The link queue alone cannot prove site size; only a fully read, non-empty sitemap whose URLs were
        # all crawled corroborates it, and never while known language versions were left uncrawled.
        verified = scope == "full_known_scope" and sitemap_ok and not langs["languages_not_crawled"]
        confidence = "high" if verified else "medium" if scope == "full_known_scope" else "low"
        if scope != "full_known_scope":
            coverage_confidence = scope
        elif langs["languages_not_crawled"]:
            coverage_confidence = "scoped_complete_other_languages_not_crawled"
        else:
            coverage_confidence = "sitemap_verified_complete" if verified else "scoped_complete"
        pct = round(100 * (discovered - remaining) / discovered, 2) if discovered else 0.0
        return {
            "audit_scope": scope,
            "coverage_confidence": coverage_confidence,
            "requested_scope": {"start_url": self.start_url, "requested_url": self.opts.url,
                                "start_url_was_sitemap": self.start_is_sitemap, "host": self.root,
                                "scope_type": "host_with_patterns" if self.include or self.exclude else "host",
                                "start_language_segment": language_segment(self.start_url),
                                "rules": "same host (www ignored), <a href> links + redirects + sitemap URLs, "
                                         f"max_depth={self.opts.max_depth}, max_pages={limit}, robots.txt "
                                         f"{'respected' if self.opts.respect_robots_txt else 'ignored'}"},
            "crawled_scope": {"language_segments": langs["crawled_language_segments"]},
            "coverage": f"{pct}% of the {discovered} URLs discovered within the requested scope "
                        "(not a percentage of the website)",
            **{k: v for k, v in langs.items() if k != "crawled_language_segments"},
            "requested_limit": limit,
            "max_depth": self.opts.max_depth,
            "sitemap_status": sitemap_status,
            "sitemap_urls": len(self.sitemap_keys) if sitemap_ok or sitemap_status == "partial" else None,
            "sitemap_urls_exceed_limit": len(self.sitemap_keys) > limit,
            "link_discovered_urls": len(self.link_keys),
            "discovered_urls": discovered,
            "crawled_urls": self.fetched,
            "queue_remaining": remaining,
            "not_crawled_urls": remaining,  # alias kept for compatibility
            "skipped_max_depth": self.stats["skipped_depth"],
            "max_depth_reached": max((int(d) for d in self.depths if d != "sitemap_only"), default=0),
            "coverage_percent": round(100 * (discovered - remaining) / discovered, 2) if discovered else 0.0,
            "complete": scope == "full_known_scope",
            "site_coverage_verified": verified,
            "confidence": confidence,
            "stop_reason": reason,
        }

    def finalize(self, started: float, failed: bool = False) -> dict:
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
            "sitemap": self.sitemap_meta,
            "robots_status": getattr(self, "robots_status", None),
            "crawl": self.coverage(failed),
        }


def language_scope(start_url: str, root: str, seen: set[str], alternates: dict[str, set[str]]) -> dict:
    """Which language versions were crawled vs only declared via hreflang. A private-host hreflang URL is
    mapped to the same path on the crawled public host to check whether that version was crawled."""
    segs = Counter(language_segment(k) or "(none)" for k in seen)
    scheme = start_url.split("://", 1)[0]
    out, all_urls, not_crawled = [], set(), set()
    for lang, urls in sorted(alternates.items()):
        rows = []
        for u in sorted(urls):
            public = u
            if is_private_host(host_of(u)):
                parts = urlsplit(u)
                public = urlunsplit((scheme, host_of(start_url), parts.path, parts.query, ""))
            crawled = crawl_key(u) in seen or crawl_key(public) in seen
            all_urls.add(public)
            if not crawled:
                not_crawled.add(public)
            rows.append(crawled)
        prefixes = sorted({f"/{language_segment(u)}" for u in urls if language_segment(u)})
        out.append({"hreflang": lang, "url_prefixes": prefixes, "urls": len(urls), "crawled": sum(rows),
                    "examples": sorted(urls)[:3],
                    "private_host": any(is_private_host(host_of(u)) for u in urls)})
    # Name uncrawled versions by URL prefix (/th) when hreflang URLs have one, else by language code.
    missing = sorted({p for a in out if a["crawled"] == 0 and a["hreflang"] != "x-default"
                      for p in (a["url_prefixes"] or [a["hreflang"]])})
    return {"crawled_language_segments": dict(segs), "alternate_languages": out,
            "alternate_language_urls": len(all_urls), "alternate_language_urls_not_crawled": len(not_crawled),
            "alternate_language_urls_not_crawled_examples": sorted(not_crawled)[:10],
            "languages_not_crawled": missing}


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
    stats = crawl.finalize(started, failed=status == "failed")
    store.update(conn, "crawls", crawl_id, {"stats": stats})
    for stage in (indexability, link_graph, page_audit_summary):
        try:
            stage.apply(conn, crawl_id)
        except Exception:  # an analysis failure must not lose the crawl itself
            log.exception("%s analysis failed for crawl %s", stage.__name__, crawl_id)
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
