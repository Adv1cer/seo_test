"""XML sitemap / sitemap-index parsing and robots.txt discovery. Parsing is pure; fetching is async."""
import gzip
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx


def parse_sitemap(xml: bytes) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls). Namespace-agnostic; tolerates gzip and junk."""
    if xml[:2] == b"\x1f\x8b":
        xml = gzip.decompress(xml)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return [], []
    tag = root.tag.rsplit("}", 1)[-1]
    locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
    return ([], locs) if tag == "sitemapindex" else (locs, [])


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
    rp, sitemaps = parse_robots(resp.text)
    return rp, sitemaps, "ok"


async def collect_sitemap_urls(client: httpx.AsyncClient, sitemap_urls: list[str],
                               max_urls: int, max_files: int = 50) -> tuple[list[str], list[dict]]:
    """Walk sitemaps and sitemap indexes breadth-first. Returns (page_urls, per-file diagnostics)."""
    queue, seen, pages, files = list(sitemap_urls), set(), [], []
    while queue and len(seen) < max_files and len(pages) < max_urls:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            files.append({"url": url, "status": None, "error": type(exc).__name__, "urls": 0})
            continue
        found, children = parse_sitemap(resp.content) if resp.status_code == 200 else ([], [])
        files.append({"url": url, "status": resp.status_code, "content_type": resp.headers.get("content-type", ""),
                      "urls": len(found), "children": len(children)})
        pages.extend(found)
        queue.extend(children)
    return pages[:max_urls], files
