"""Roll the existing per-page audits (seo_auditor) up into compact site-level evidence. No new checks:
every issue code comes from the stored page audit, plus cross-page duplicate title / meta description
which can only be seen across pages. Only pages actually crawled and audited (crawl_status ok) count."""
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from app.rules.seo_rules import RULES, SEVERITY_ORDER, SITE_RULES
from app.services import root_causes, store
from app.services.url_utils import resource_type

_EXAMPLES = 5
_VALUE_CHARS = 160


def _path(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _short(value):
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= _VALUE_CHARS else text[:_VALUE_CHARS] + "…"


def summarize(pages: list[dict]) -> dict:
    """pages: rows with url, title, meta_description, audit (dict). Returns {total_pages, issues}."""
    total = len(pages)
    hits: dict[str, list[dict]] = defaultdict(list)
    meta: dict[str, dict] = {}
    rc_hits, explained = root_causes.explain(pages)
    for p in pages:
        for issue in (p.get("audit") or {}).get("issues", []):
            if issue["severity"] == "passed":
                continue
            meta.setdefault(issue["code"], issue)
            example = {"url": _path(p["url"]), "detail": issue["message"],
                       "_root_cause": explained.get((p["url"], issue["code"]))}
            if issue.get("value") not in (None, "", []):
                example["value"] = _short(issue["value"])
            hits[issue["code"]].append(example)

    for code, field in (("DUPLICATE_TITLE", "title"), ("DUPLICATE_META_DESCRIPTION", "meta_description")):
        groups = defaultdict(list)
        for p in pages:
            if p.get(field):
                groups[p[field].strip().casefold()].append(p)
        dup_groups = sorted((g for g in groups.values() if len(g) > 1), key=len, reverse=True)
        for g in dup_groups:
            for p in g:
                hits[code].append({"url": _path(p["url"]), "value": _short(p[field]), "shared_with": len(g) - 1})
        if dup_groups:
            category, severity, rec = SITE_RULES[code]
            meta[code] = {"category": category, "severity": severity, "recommendation": rec,
                          "message": f"{len(dup_groups)} group(s) of pages share the same {field.replace('_', ' ')}."}

    issues = []
    for code, examples in hits.items():
        m = meta[code]
        category, severity, rec = RULES.get(code) or SITE_RULES.get(code) or (m["category"], m["severity"],
                                                                               m.get("recommendation", ""))
        by_rc = Counter(e.pop("_root_cause", None) for e in examples)
        independent = by_rc.pop(None, 0)
        issues.append({
            "issue": code, "category": category, "severity": severity,
            "count": len(examples), "total_pages": total,
            "percentage": round(100 * len(examples) / total, 2) if total else 0.0,
            # Pages where this detection is explained by a root cause are counted there, not here.
            "independent_count": independent,
            "explained_by_root_cause": dict(by_rc),
            "report_separately": independent > 0,
            "description": m["message"] if code in SITE_RULES else
            f"{len(examples)} of {total} crawled page(s) have {code}.",
            "recommendation": rec,
            "examples": examples[:_EXAMPLES],
            # relative to /api/seo/crawls/{crawl_id}/ — lists every affected page with its full audit
            "drilldown": f"pages?issue={code}&include_audit=true" if code in RULES else None,
        })
    issues.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], -i["count"], i["issue"]))
    return {"total_pages": total, "denominator": _DENOMINATOR,
            "root_causes": root_causes.summarize(rc_hits, total, _path), "issues": issues}


_DENOMINATOR = ("total_pages = HTML documents fetched successfully (HTTP < 400) and audited. Excludes redirects, "
                "error pages, failed requests, robots-blocked URLs and image/file assets. Every percentage = "
                "count / total_pages.")


def apply(conn, crawl_id: int) -> None:
    rows = [store.row_dict(r) for r in conn.execute(
        "SELECT url, title, meta_description, audit, render_info, content_type, status_code FROM crawl_pages WHERE crawl_id = ? AND crawl_status = 'ok' "
        "AND audit IS NOT NULL AND (status_code IS NULL OR status_code < 400) ORDER BY depth IS NULL, depth, id",
        (crawl_id,))
        if resource_type(r["url"], r["content_type"], r["status_code"]) == "document"]
    crawl = store.row_dict(conn.execute("SELECT stats FROM crawls WHERE id = ?", (crawl_id,)).fetchone())
    stats = {**(crawl.get("stats") or {}), "page_audit_summary": summarize(rows)}
    store.update(conn, "crawls", crawl_id, {"stats": stats})
    conn.commit()
