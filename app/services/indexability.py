"""Indexability engine: per-page states (with reasons) and site-level sitemap/indexability issues,
computed after a crawl from stored rows. Pure functions + one apply() that reads/writes SQLite."""
import re
from collections import Counter

from app.models.response import Issue
from app.rules.seo_rules import SEVERITY_ORDER, SITE_RULES
from app.services import store
from app.services.url_utils import crawl_key, host_of, is_private_host, is_web_url, resource_type

STATES = ("discovered", "crawlable", "fetchable", "renderable", "indexable", "canonical")
_SOFT_404 = re.compile(r"\b(404|not found|page not found|page doesn.t exist)\b|ไม่พบ(หน้า)?", re.I)
_EXAMPLES = 20


def _state(value: bool | None, reason: str, status: str | None = None) -> dict:
    return {"value": value, "reason": reason} | ({"status": status} if status else {})


def _noindex(directives: str | None) -> bool:
    """True if a meta robots / X-Robots-Tag value contains noindex or none (any user-agent prefix)."""
    tokens = re.split(r"[,\s]+", (directives or "").lower())
    return any(t.split(":")[-1] in ("noindex", "none") for t in tokens)


def page_states(row: dict, pages_by_key: dict[str, dict]) -> dict:
    """States for one crawled page. value None = not determinable (e.g. never fetched)."""
    status, code = row["crawl_status"], row["status_code"]
    via = row["discovered_via"]
    s = {"soft_404_candidate": False, "discovered": _state(True, f"via {via}" + (" (also in sitemap)" if row["in_sitemap"] and via != "sitemap" else ""))}

    if status == "blocked_robots":
        s["crawlable"] = _state(False, "blocked by robots.txt")
        for k in ("fetchable", "renderable", "indexable", "canonical"):
            s[k] = _state(None, "not crawled (blocked by robots.txt)")
        s["indexable"] = _state(False, "blocked by robots.txt (cannot be crawled; may still be indexed URL-only)")
        return s
    s["crawlable"] = _state(True, "allowed by robots.txt")

    if status == "error":
        s["fetchable"] = _state(False, row["error"] or "request failed")
    elif status == "redirect":
        s["fetchable"] = _state(True, f"HTTP {code} redirect")
    elif code is not None and code >= 400:
        s["fetchable"] = _state(False, f"HTTP {code}")
    else:
        s["fetchable"] = _state(True, f"HTTP {code}")

    kind = resource_type(row["url"], row["content_type"], code)
    if kind != "document":  # image/font/PDF/...: a resource, not a page; only fetchability applies
        for k in ("renderable", "indexable", "canonical"):
            s[k] = _state(None, f"non-page resource ({kind}, content-type {row['content_type'] or 'unknown'})")
        s["canonical"]["status"] = "not_applicable"
        s["resource"] = kind
        return s
    s["resource"] = "document"

    render = row["render_status"]
    if status != "ok":
        s["renderable"] = _state(None, "not analyzed")
    elif render == "failed":
        s["renderable"] = _state(False, "browser rendering failed")
    elif render == "skipped":
        s["renderable"] = _state(None, "rendering needed but disabled")
    else:
        s["renderable"] = _state(True, "rendered in browser" if render == "rendered" else "content present in HTML")

    directives = row["robots_directives"] or {}
    if not s["fetchable"]["value"]:
        s["indexable"] = _state(False, s["fetchable"]["reason"])
    elif status == "redirect":
        s["indexable"] = _state(False, f"redirects to {row['redirect_target']}")
    elif status == "non_html":
        s["indexable"] = _state(None, f"non-HTML content ({row['content_type']}) not analyzed")
    elif _noindex(directives.get("x_robots_tag")):
        s["indexable"] = _state(False, "X-Robots-Tag noindex")
    elif _noindex(directives.get("meta")):
        s["indexable"] = _state(False, "meta robots noindex")
    else:
        s["indexable"] = _state(True, "HTTP 200, no robots exclusion")

    # canonical.value answers ONE question: "is this page its own valid canonical?" It says nothing about
    # whether the page itself is indexable (that is the indexable state). canonical.status says why.
    canonical = row["canonical_url"]
    if status != "ok" or (code or 0) >= 400:
        s["canonical"] = _state(None, "not analyzed (page not successfully fetched)", "not_analyzed")
    elif not canonical:
        s["canonical"] = _state(True, "no canonical tag (self-canonical by default)", "missing_implicit_self")
    elif not is_web_url(canonical):
        s["canonical"] = _state(False, f"canonical is not a valid http(s) URL: {canonical}", "invalid")
    elif crawl_key(canonical) == row["normalized_url"]:
        s["canonical"] = _state(True, "self-referencing canonical", "self")
    elif is_private_host(host_of(canonical)):
        s["canonical"] = _state(False, f"canonical points to private/internal host: {canonical}", "private_host")
    else:
        target = pages_by_key.get(crawl_key(canonical))
        reason = f"canonical points to another URL: {canonical}"
        if target is not None and (target["crawl_status"] != "ok" or (target["status_code"] or 0) >= 400):
            reason += f" (target is {target['crawl_status']}, HTTP {target['status_code']})"
            s["canonical"] = _state(False, reason, "target_not_indexable")
        else:
            s["canonical"] = _state(False, reason, "other_url")

    title_h1 = f"{row['title'] or ''} {row['h1'] or ''}"
    s["soft_404_candidate"] = bool(status == "ok" and code == 200 and _SOFT_404.search(title_h1))
    return s


def _issue(code: str, message: str, urls: list[str] | None = None, value=None) -> Issue:
    category, severity, rec = SITE_RULES[code]
    count = len(urls) if urls is not None else 1
    return Issue(code=code, category=category, severity=severity, message=message, count=count,
                 value=value if value is not None else (urls[:_EXAMPLES] if urls else None), recommendation=rec)


def site_issues(crawl: dict, rows: list[dict], states: dict[str, dict]) -> list[Issue]:
    issues: list[Issue] = []
    stats, robots = crawl.get("stats") or {}, crawl.get("robots") or {}
    files = stats.get("sitemap_files") or []
    sitemap_urls = crawl.get("sitemap_urls") or []
    options = crawl.get("options") or {}

    if robots.get("status") == "html_response":
        issues.append(_issue("ROBOTS_TXT_INVALID", "/robots.txt returns an HTML page instead of a plain-text "
                             "robots.txt, so it was treated as absent (no rules, no Sitemap: directive)."))

    if options.get("use_sitemaps", True):
        meta = stats.get("sitemap") or {}
        status = meta.get("status")
        file_info = [{"url": f["url"], "status": f.get("status"), "content_type": f.get("content_type"),
                      "result": f.get("result")} for f in files]
        empty_messages = {
            "html_instead_of_xml": "Sitemap URL returns an HTML page instead of an XML sitemap.",
            "parse_error": "Sitemap could not be parsed as XML.",
            "unsupported_format": "Sitemap is XML but not a <urlset>/<sitemapindex> (e.g. an RSS feed).",
            "empty": "Sitemap is valid XML but contains 0 <loc> URLs.",
        }
        if status in ("not_found", "fetch_error") or (status is None and not any(f.get("status") == 200 for f in files)):
            issues.append(_issue("SITEMAP_NOT_FOUND", "No reachable XML sitemap found.", value=file_info))
        elif status in empty_messages:
            issues.append(_issue("SITEMAP_EMPTY_OR_INVALID", empty_messages[status], value=file_info))
        elif status is None and not sitemap_urls and not any(f.get("children") for f in files):
            issues.append(_issue("SITEMAP_EMPTY_OR_INVALID",
                                 "Sitemap responds with HTTP 200 but contains no <loc> URLs.", value=file_info))
        if robots.get("status") == "ok" and not robots.get("sitemaps"):
            issues.append(_issue("SITEMAP_NOT_IN_ROBOTS", "robots.txt has no Sitemap: directive."))

        # sitemap_urls is deduplicated at read time; duplicates are reported in the sitemap meta.
        # Crawls without meta (older rows) fall back to counting the raw list.
        dupes = meta.get("duplicate_examples") if meta else \
            [u for u, n in Counter(crawl_key(u) for u in sitemap_urls).items() if n > 1]
        if dupes:
            n_dupes = meta.get("duplicate_urls", len(dupes))
            issues.append(_issue("SITEMAP_DUPLICATE_URLS", f"{n_dupes} URL(s) listed more than once in sitemaps.", dupes))
            issues[-1].count = n_dupes

        in_sitemap = [r for r in rows if r["in_sitemap"]]
        groups = {
            "SITEMAP_URL_NON_200": [r["url"] for r in in_sitemap if r["crawl_status"] == "error"
                                    or (r["status_code"] or 0) >= 400],
            "SITEMAP_URL_REDIRECT": [r["url"] for r in in_sitemap if r["crawl_status"] == "redirect"],
            "SITEMAP_URL_BLOCKED": [r["url"] for r in in_sitemap if r["crawl_status"] == "blocked_robots"],
            "SITEMAP_URL_NOINDEX": [r["url"] for r in in_sitemap if "noindex" in
                                    (states[r["normalized_url"]]["indexable"]["reason"] or "")],
            "SITEMAP_URL_CANONICALIZED": [r["url"] for r in in_sitemap
                                          if states[r["normalized_url"]]["canonical"]["value"] is False],
        }
        labels = {"SITEMAP_URL_NON_200": "return errors", "SITEMAP_URL_REDIRECT": "redirect",
                  "SITEMAP_URL_BLOCKED": "are blocked by robots.txt", "SITEMAP_URL_NOINDEX": "are noindex",
                  "SITEMAP_URL_CANONICALIZED": "canonicalize to another URL"}
        for code, urls in groups.items():
            if urls:
                issues.append(_issue(code, f"{len(urls)} sitemap URL(s) {labels[code]}.", urls))

        # Only conclusive when every sitemap file was read; a truncated read would produce false positives.
        complete = bool(meta.get("read_complete"))
        if sitemap_urls and not complete:
            issues.append(_issue(
                "SITEMAP_ANALYSIS_INCOMPLETE",
                f"Sitemap analysis is incomplete ({meta.get('stop_reason') or 'unknown read status'}), so "
                "missing-from-sitemap findings were not evaluated.",
                value={k: meta.get(k) for k in ("discovered_sitemaps", "discovered_urls", "stop_reason",
                                                 "failed_sitemaps", "limits")}))
        elif sitemap_urls:
            missing = [r["url"] for r in rows if not r["in_sitemap"] and r["crawl_status"] == "ok"
                       and states[r["normalized_url"]]["indexable"]["value"]
                       and states[r["normalized_url"]]["canonical"]["value"]]
            if missing:
                issues.append(_issue("PAGES_MISSING_FROM_SITEMAP",
                                     f"{len(missing)} indexable, self-canonical page(s) are not in the sitemap.", missing))

    def urls_where(pred):
        return [r["url"] for r in rows if pred(r, states[r["normalized_url"]])]

    noindex = urls_where(lambda r, s: "noindex" in (s["indexable"]["reason"] or ""))
    if noindex:
        issues.append(_issue("NOINDEX_PAGES", f"{len(noindex)} page(s) are noindex.", noindex))

    # Broken image/file URLs are reported by the link graph as BROKEN_RESOURCE_LINKS, not as pages.
    def is_broken_doc(r, s):
        return s["fetchable"]["value"] is False and (s.get("resource") or "document") == "document"
    broken = urls_where(is_broken_doc)
    if broken:
        issues.append(_issue("BROKEN_PAGES", f"{len(broken)} discovered page URL(s) return errors or fail to load.",
                             value=[{"url": r["url"], "reason": states[r["normalized_url"]]["fetchable"]["reason"],
                                     "linked_from": r["parent_url"]}
                                    for r in rows if is_broken_doc(r, states[r["normalized_url"]])][:_EXAMPLES]))
        issues[-1].count = len(broken)

    blocked = urls_where(lambda r, s: s["crawlable"]["value"] is False)
    if blocked:
        issues.append(_issue("ROBOTS_BLOCKED_PAGES", f"{len(blocked)} linked URL(s) are blocked by robots.txt.", blocked))

    render_failed = urls_where(lambda r, s: s["renderable"]["value"] is False)
    if render_failed:
        issues.append(_issue("RENDER_FAILED_PAGES", f"{len(render_failed)} page(s) failed to render.", render_failed))

    soft = urls_where(lambda r, s: s["soft_404_candidate"])
    if soft:
        issues.append(_issue("SOFT_404_CANDIDATES", f"{len(soft)} page(s) look like error pages but return 200.", soft))

    # Canonical pointing at something that cannot be indexed is critical; plain cross-URL canonicals are info.
    bad_status = ("private_host", "target_not_indexable", "invalid")
    bad_target = urls_where(lambda r, s: s["canonical"].get("status") in bad_status)
    if bad_target:
        bad = set(bad_target)
        bad_rows = [r for r in rows if r["url"] in bad]
        by_status = Counter(states[r["normalized_url"]]["canonical"]["status"] for r in bad_rows)
        still_indexable = sum(1 for r in bad_rows if states[r["normalized_url"]]["indexable"]["value"])
        examples = sorted({states[r["normalized_url"]]["canonical"]["reason"] for r in bad_rows})
        issues.append(_issue(
            "CANONICAL_TO_NON_INDEXABLE",
            f"{len(bad_target)} page(s) declare a canonical URL search engines cannot use "
            f"({', '.join(f'{n} {k}' for k, n in sorted(by_status.items()))}). {still_indexable} of these pages "
            "are themselves fetchable and not noindex; the defect is the canonical declaration, which search "
            "engines will likely ignore or which can send the wrong canonical signal.",
            bad_target, value={"urls": bad_target[:_EXAMPLES], "reasons": examples[:_EXAMPLES],
                               "canonical_status": dict(by_status), "pages_still_indexable": still_indexable}))
        issues[-1].count = len(bad_target)
    other = [u for u in urls_where(lambda r, s: s["canonical"]["value"] is False) if u not in bad_target]
    if other:
        issues.append(_issue("CANONICALIZED_PAGES", f"{len(other)} page(s) canonicalize to another URL.", other))

    return sorted(issues, key=lambda i: (SEVERITY_ORDER[i.severity], i.category, i.code))


def summarize(states: dict[str, dict]) -> dict:
    """Count true/false/unknown per state across pages."""
    out = {}
    for name in STATES:
        c = Counter({True: "true", False: "false", None: "unknown"}[s[name]["value"]] for s in states.values())
        out[name] = {"true": c["true"], "false": c["false"], "unknown": c["unknown"]}
    out["canonical_status"] = dict(Counter(s["canonical"].get("status", "not_analyzed") for s in states.values()))
    out["resource_type"] = dict(Counter(s.get("resource", "not_fetched") for s in states.values()))
    return out


def url_counts(rows: list[dict]) -> dict:
    """Explicit denominators. Every discovered/recorded URL falls in exactly one bucket."""
    c = Counter()
    for r in rows:
        status, code = r["crawl_status"], r["status_code"] or 0
        asset = resource_type(r["url"], r["content_type"], r["status_code"]) != "document"
        if status == "blocked_robots":
            c["blocked_by_robots"] += 1
        elif status == "redirect":
            c["redirects"] += 1
        elif status == "error":
            c["failed_requests"] += 1
        elif asset or status == "non_html":
            c["assets_broken" if code >= 400 else "assets_and_non_html_ok"] += 1
        elif code >= 400:
            c["html_error_pages"] += 1
        else:
            c["html_pages_ok"] += 1
    keys = ("html_pages_ok", "html_error_pages", "assets_and_non_html_ok", "assets_broken", "redirects",
            "failed_requests", "blocked_by_robots")
    return {"recorded_urls": len(rows), **{k: c[k] for k in keys},
            "broken_urls": c["html_error_pages"] + c["assets_broken"] + c["failed_requests"]}


def apply(conn, crawl_id: int) -> None:
    """Compute and persist page states and site issues for a finished crawl."""
    crawl = store.row_dict(conn.execute("SELECT * FROM crawls WHERE id = ?", (crawl_id,)).fetchone())
    rows = [store.row_dict(r) for r in conn.execute("SELECT * FROM crawl_pages WHERE crawl_id = ?", (crawl_id,))]
    by_key = {r["normalized_url"]: r for r in rows}
    states = {k: page_states(r, by_key) for k, r in by_key.items()}
    conn.executemany("UPDATE crawl_pages SET indexability = ? WHERE id = ?",
                     [(store._dump(states[r["normalized_url"]]), r["id"]) for r in rows])
    issues = site_issues(crawl, rows, states)
    stats = {**(crawl.get("stats") or {}), "indexability": summarize(states), "counts": url_counts(rows)}
    store.update(conn, "crawls", crawl_id, {"site_issues": [i.model_dump() for i in issues], "stats": stats})
    conn.commit()
