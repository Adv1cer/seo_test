"""Indexability engine: per-page states (with reasons) and site-level sitemap/indexability issues,
computed after a crawl from stored rows. Pure functions + one apply() that reads/writes SQLite."""
import re
from collections import Counter

from app.models.response import Issue
from app.rules.seo_rules import SEVERITY_ORDER, SITE_RULES
from app.services import store
from app.services.url_utils import crawl_key, host_of, is_private_host, is_web_url

STATES = ("discovered", "crawlable", "fetchable", "renderable", "indexable", "canonical")
_SOFT_404 = re.compile(r"\b(404|not found|page not found|page doesn.t exist)\b|ไม่พบ(หน้า)?", re.I)
_EXAMPLES = 20


def _state(value: bool | None, reason: str) -> dict:
    return {"value": value, "reason": reason}


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

    canonical = row["canonical_url"]
    if status != "ok":
        s["canonical"] = _state(None, "not analyzed")
    elif not canonical:
        s["canonical"] = _state(True, "no canonical tag (self-canonical by default)")
    elif not is_web_url(canonical):
        s["canonical"] = _state(False, f"canonical is not a valid http(s) URL: {canonical}")
    elif crawl_key(canonical) == row["normalized_url"]:
        s["canonical"] = _state(True, "self-referencing canonical")
    elif is_private_host(host_of(canonical)):
        s["canonical"] = _state(False, f"canonical points to private/internal host: {canonical}")
    else:
        target = pages_by_key.get(crawl_key(canonical))
        reason = f"canonical points to another URL: {canonical}"
        if target is not None and (target["crawl_status"] != "ok" or (target["status_code"] or 0) >= 400):
            reason += f" (target is {target['crawl_status']}, HTTP {target['status_code']})"
        s["canonical"] = _state(False, reason)

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

    if options.get("use_sitemaps", True):
        ok_files = [f for f in files if f.get("status") == 200]
        if not ok_files:
            issues.append(_issue("SITEMAP_NOT_FOUND", "No reachable XML sitemap found.",
                                 value=[{"url": f["url"], "status": f.get("status")} for f in files]))
        elif not sitemap_urls and not any(f.get("children") for f in ok_files):
            issues.append(_issue("SITEMAP_EMPTY_OR_INVALID",
                                 "Sitemap responds with HTTP 200 but contains no <loc> URLs.",
                                 value=[{"url": f["url"], "content_type": f.get("content_type")} for f in ok_files]))
        if robots.get("status") == "ok" and not robots.get("sitemaps"):
            issues.append(_issue("SITEMAP_NOT_IN_ROBOTS", "robots.txt has no Sitemap: directive."))

        dupes = [u for u, n in Counter(crawl_key(u) for u in sitemap_urls).items() if n > 1]
        if dupes:
            issues.append(_issue("SITEMAP_DUPLICATE_URLS", f"{len(dupes)} URL(s) listed more than once in sitemaps.", dupes))

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

        if sitemap_urls:  # only meaningful when a sitemap exists
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

    broken = urls_where(lambda r, s: s["fetchable"]["value"] is False)
    if broken:
        issues.append(_issue("BROKEN_PAGES", f"{len(broken)} discovered URL(s) return errors or fail to load.",
                             value=[{"url": r["url"], "reason": states[r["normalized_url"]]["fetchable"]["reason"],
                                     "linked_from": r["parent_url"]}
                                    for r in rows if states[r["normalized_url"]]["fetchable"]["value"] is False][:_EXAMPLES]))
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
    bad_target = urls_where(lambda r, s: s["canonical"]["value"] is False and (
        "private" in s["canonical"]["reason"] or "target is" in s["canonical"]["reason"]
        or "not a valid" in s["canonical"]["reason"]))
    if bad_target:
        bad = set(bad_target)
        examples = sorted({states[r["normalized_url"]]["canonical"]["reason"] for r in rows if r["url"] in bad})
        issues.append(_issue("CANONICAL_TO_NON_INDEXABLE",
                             f"{len(bad_target)} page(s) have a canonical pointing to a URL that cannot be indexed.",
                             bad_target, value={"urls": bad_target[:_EXAMPLES], "reasons": examples[:_EXAMPLES]}))
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
    return out


def apply(conn, crawl_id: int) -> None:
    """Compute and persist page states and site issues for a finished crawl."""
    crawl = store.row_dict(conn.execute("SELECT * FROM crawls WHERE id = ?", (crawl_id,)).fetchone())
    rows = [store.row_dict(r) for r in conn.execute("SELECT * FROM crawl_pages WHERE crawl_id = ?", (crawl_id,))]
    by_key = {r["normalized_url"]: r for r in rows}
    states = {k: page_states(r, by_key) for k, r in by_key.items()}
    conn.executemany("UPDATE crawl_pages SET indexability = ? WHERE id = ?",
                     [(store._dump(states[r["normalized_url"]]), r["id"]) for r in rows])
    issues = site_issues(crawl, rows, states)
    stats = {**(crawl.get("stats") or {}), "indexability": summarize(states)}
    store.update(conn, "crawls", crawl_id, {"site_issues": [i.model_dump() for i in issues], "stats": stats})
    conn.commit()
