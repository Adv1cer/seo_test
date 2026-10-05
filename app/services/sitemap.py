"""XML sitemap / sitemap-index parsing and robots.txt discovery. Parsing is pure; fetching is async."""
import gzip
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx

from app.services.url_utils import crawl_key, is_web_url


def _looks_html(body: bytes, content_type: str = "") -> bool:
    head = body[:512].lstrip().lower()
    return "html" in content_type.lower() or head.startswith((b"<!doctype html", b"<html"))


def classify_sitemap(xml: bytes, content_type: str = "") -> tuple[list[str], list[str], str]:
    """(page_urls, child_sitemap_urls, result). result: ok | empty | html | parse_error | unsupported_format.
    Namespace-agnostic; tolerates gzip."""
    try:
        if xml[:2] == b"\x1f\x8b":
            xml = gzip.decompress(xml)
    except (OSError, EOFError):
        return [], [], "parse_error"
    if _looks_html(xml, content_type):
        return [], [], "html"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return [], [], "parse_error"
    tag = root.tag.rsplit("}", 1)[-1]
    if tag not in ("urlset", "sitemapindex"):
        return [], [], "unsupported_format"  # e.g. RSS/Atom feeds: not read, so the inventory has a gap
    locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
    result = "ok" if locs else "empty"
    return ([], locs, result) if tag == "sitemapindex" else (locs, [], result)


def parse_sitemap(xml: bytes) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls)."""
    urls, children, _ = classify_sitemap(xml)
    return urls, children


def parse_robots(text: str) -> tuple[RobotFileParser, list[str]]:
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    sitemaps = [line.split(":", 1)[1].strip() for line in text.splitlines()
                if line.lower().startswith("sitemap:")]
    return rp, sitemaps


async def fetch_robots(client: httpx.AsyncClient, root: str) -> tuple[RobotFileParser | None, list[str], str]:
    """(parser or None if absent/unreachable → allow all, sitemap URLs, status note)."""
    url = urljoin(root, "/robots.txt")
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        return None, [], f"unreachable: {type(exc).__name__}"
    if resp.status_code >= 400:
        return None, [], f"HTTP {resp.status_code}"
    if _looks_html(resp.content, resp.headers.get("content-type", "")):
        return None, [], "html_response"  # SPA fallback page, not a robots.txt: treat as absent
    rp, sitemaps = parse_robots(resp.text)
    return rp, sitemaps, "ok"


async def collect_sitemap_urls(client: httpx.AsyncClient, sitemap_urls: list[str], max_urls: int,
                               max_files: int) -> tuple[list[str], list[dict], dict]:
    """Walk sitemaps and (nested) sitemap indexes breadth-first, reading every file unless a sitemap
    safety limit is hit. Independent of the crawl's max_pages. Returns (deduplicated page URLs,
    per-file diagnostics, completeness meta). read_complete is False whenever any listed sitemap
    went unread (limit or fetch failure), so "missing from sitemap" cannot be concluded."""
    queue, seen, files = list(dict.fromkeys(sitemap_urls)), set(), []
    pages: dict[str, str] = {}   # crawl_key -> first URL as listed
    dupes: dict[str, int] = {}
    stop_reason = None
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        if len(seen) >= max_files or len(pages) >= max_urls:
            stop_reason = "sitemap_safety_limit"
            break
        seen.add(url)
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            files.append({"url": url, "status": None, "error": type(exc).__name__, "urls": 0,
                          "result": "fetch_error"})
            continue
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200:
            found, children, result = classify_sitemap(resp.content, ctype)
        else:
            found, children, result = [], [], f"http_{resp.status_code}"
        files.append({"url": url, "status": resp.status_code, "content_type": ctype,
                      "urls": len(found), "children": len(children), "result": result})
        for u in found:
            key = crawl_key(u) if is_web_url(u) else u
            if key in pages:
                dupes[key] = dupes.get(key, 1) + 1
            elif len(pages) < max_urls:
                pages[key] = u
            else:
                stop_reason = "sitemap_safety_limit"
        queue.extend(c for c in children if c not in seen)
    status, stop_reason = _overall_status(files, bool(pages), stop_reason)
    meta = {"status": status, "discovered_sitemaps": len(files), "discovered_urls": len(pages),
            "read_complete": status in ("ok", "empty"), "stop_reason": stop_reason,
            "failed_sitemaps": [{"url": f["url"], "result": f["result"]} for f in files
                                if f["result"] not in ("ok", "empty")][:20],
            "duplicate_urls": len(dupes), "duplicate_examples": list(dupes)[:20],
            "limits": {"max_urls": max_urls, "max_files": max_files}}
    return list(pages.values()), files, meta


# Per-file result -> (overall status when no URLs were read, stop_reason). Order = priority.
_PROBLEMS = [("html", "html_instead_of_xml", "sitemap_html_response"),
             ("parse_error", "parse_error", "sitemap_parse_error"),
             ("unsupported_format", "unsupported_format", "sitemap_unsupported_format"),
             ("fetch_error", "fetch_error", "sitemap_fetch_error"),
             ("http_", "not_found", "sitemap_http_error")]


def _overall_status(files: list[dict], has_urls: bool, limit_hit: str | None) -> tuple[str, str | None]:
    """ok: every file read, N>0 URLs | empty: every file read, 0 URLs | partial: some URLs read but a
    file failed or a limit hit | html_instead_of_xml / parse_error / unsupported_format / fetch_error /
    not_found: nothing usable read."""
    results = [f["result"] for f in files]
    problem = next(((st, reason) for prefix, st, reason in _PROBLEMS
                    if any(r.startswith(prefix) for r in results)), None)
    if limit_hit:
        return "partial", limit_hit
    if problem is None:
        return ("ok" if has_urls else "empty"), None
    return ("partial" if has_urls else problem[0]), problem[1]
