"""Internal link graph analysis over a finished crawl: authority (simplified PageRank), orphan /
dead-end / deep / weakly-linked pages, and broken / redirected internal links."""
from collections import Counter, defaultdict

from app.config import settings
from app.models.response import Issue
from app.rules.seo_rules import SEVERITY_ORDER, SITE_RULES
from app.services import store
from app.services.url_utils import crawl_key

_EXAMPLES = 20
DAMPING = 0.85


def pagerank(nodes: list[str], edges: dict[str, set[str]], iterations: int = 50, tol: float = 1e-8) -> dict[str, float]:
    """Plain power-iteration PageRank. edges: source -> set of targets (all must be in nodes).
    Dangling nodes spread their rank uniformly."""
    n = len(nodes)
    if n == 0:
        return {}
    rank = dict.fromkeys(nodes, 1 / n)
    for _ in range(iterations):
        dangling = sum(rank[u] for u in nodes if not edges.get(u))
        new = dict.fromkeys(nodes, (1 - DAMPING) / n + DAMPING * dangling / n)
        for src, targets in edges.items():
            if targets:
                share = DAMPING * rank[src] / len(targets)
                for t in targets:
                    new[t] += share
        delta = sum(abs(new[u] - rank[u]) for u in nodes)
        rank = new
        if delta < tol:
            break
    return rank


def _issue(code: str, message: str, value, count: int) -> Issue:
    category, severity, rec = SITE_RULES[code]
    return Issue(code=code, category=category, severity=severity, message=message, value=value,
                 count=count, recommendation=rec)


def analyze(rows: list[dict], links: list[dict]) -> tuple[dict[str, dict], list[Issue], dict]:
    """rows: crawl_pages; links: internal page_links. Returns (metrics per normalized url, issues, summary)."""
    by_key = {r["normalized_url"]: r for r in rows}
    # Pages that can hold/pass authority: fetched HTML with a non-error status.
    live = {k for k, r in by_key.items() if r["crawl_status"] == "ok" and (r["status_code"] or 0) < 400}
    redirect_to = {k: r["redirect_target"] for k, r in by_key.items() if r["crawl_status"] == "redirect"}

    def resolve(target: str, hops: int = 5) -> str:
        while target in redirect_to and hops:
            target, hops = crawl_key(redirect_to[target]), hops - 1
        return target

    followed: dict[str, set[str]] = defaultdict(set)
    inlinks: dict[str, set[str]] = defaultdict(set)
    broken, redirected = [], []
    anchor_locations = Counter()
    for l in links:
        src, tgt = l["source_url"], l["target_url"]
        if src == tgt or src not in by_key:
            continue
        anchor_locations[l.get("location") or "body"] += 1
        target_row = by_key.get(tgt)
        if target_row is not None:
            if target_row["crawl_status"] == "error" or (target_row["status_code"] or 0) >= 400:
                broken.append({"source": by_key[src]["url"], "target": target_row["url"], "anchor": l["anchor_text"],
                               "status": target_row["status_code"] or target_row["crawl_status"]})
            elif target_row["crawl_status"] == "redirect":
                redirected.append({"source": by_key[src]["url"], "target": target_row["url"],
                                   "redirects_to": target_row["redirect_target"], "anchor": l["anchor_text"]})
        final = resolve(tgt)
        if l["nofollow"] or final == src:
            continue
        inlinks[final].add(src)
        if src in live and final in live:
            followed[src].add(final)

    ranks = pagerank(sorted(live), followed)
    top = max(ranks.values(), default=0) or 1
    root = next((k for k, r in by_key.items() if r["discovered_via"] == "root"), None)
    deep_at = settings.deep_page_depth

    metrics: dict[str, dict] = {}
    for k, r in by_key.items():
        if k not in live:
            continue
        unique_in = len(inlinks.get(k, set()) - {k})
        unique_out = len(followed.get(k, set()))
        authority = round(100 * ranks.get(k, 0) / top, 1)
        flags = []
        if k != root and unique_in == 0:
            flags.append("orphan_candidate")
        elif k != root and unique_in == 1:
            flags.append("weak_internal_linking")
        if unique_out == 0:
            flags.append("dead_end")
        if r["depth"] is not None and r["depth"] >= deep_at:
            flags.append("deep")
        metrics[k] = {"authority": authority, "unique_inlinks": unique_in, "unique_outlinks": unique_out,
                      "flags": flags}

    # "Important" = top 20% by authority, or listed in the sitemap.
    cutoff = sorted((m["authority"] for m in metrics.values()), reverse=True)
    cutoff = cutoff[max(0, len(cutoff) // 5 - 1)] if cutoff else 0
    for k, m in metrics.items():
        if "deep" in m["flags"] and (m["authority"] >= cutoff or by_key[k]["in_sitemap"]):
            m["flags"].append("important_but_deep")

    def flagged(flag):
        hits = sorted(((by_key[k]["url"], m) for k, m in metrics.items() if flag in m["flags"]),
                      key=lambda x: -x[1]["authority"])
        return [u for u, _ in hits]

    issues: list[Issue] = []
    if broken:
        issues.append(_issue("BROKEN_INTERNAL_LINKS",
                             f"{len(broken)} internal link(s) point to error pages "
                             f"({len({b['target'] for b in broken})} distinct target(s)).",
                             broken[:_EXAMPLES], len(broken)))
    if redirected:
        issues.append(_issue("REDIRECTED_INTERNAL_LINKS", f"{len(redirected)} internal link(s) go through redirects.",
                             redirected[:_EXAMPLES], len(redirected)))
    for code, flag, text in [
        ("ORPHAN_PAGE_CANDIDATES", "orphan_candidate", "have no followed internal links from crawled pages"),
        ("DEAD_END_PAGES", "dead_end", "have no followed internal links to other pages"),
        ("WEAKLY_LINKED_PAGES", "weak_internal_linking", "are linked from only one other page"),
        ("IMPORTANT_PAGES_TOO_DEEP", "important_but_deep", f"are important but {deep_at}+ clicks from the start page"),
        ("DEEP_PAGES", "deep", f"are {deep_at}+ clicks from the start page"),
    ]:
        urls = flagged(flag)
        if urls:
            issues.append(_issue(code, f"{len(urls)} page(s) {text}.", urls[:_EXAMPLES], len(urls)))

    depth = Counter("sitemap_only" if by_key[k]["depth"] is None else by_key[k]["depth"] for k in metrics)
    summary = {
        "pages": len(metrics),
        "internal_links": sum(len(v) for v in followed.values()),
        "avg_unique_inlinks": round(sum(m["unique_inlinks"] for m in metrics.values()) / len(metrics), 1) if metrics else 0,
        "max_depth": max((d for d in depth if d != "sitemap_only"), default=0),
        "depth_distribution": {str(k): v for k, v in sorted(depth.items(), key=lambda x: (isinstance(x[0], str), x[0]))},
        "link_locations": dict(anchor_locations),
        "broken_internal_links": len(broken),
        "redirected_internal_links": len(redirected),
        **{f"{flag}_count": len(flagged(flag)) for flag in
           ("orphan_candidate", "dead_end", "weak_internal_linking", "deep", "important_but_deep")},
    }
    return metrics, sorted(issues, key=lambda i: (SEVERITY_ORDER[i.severity], i.code)), summary


def apply(conn, crawl_id: int) -> None:
    rows = [store.row_dict(r) for r in conn.execute("SELECT * FROM crawl_pages WHERE crawl_id = ?", (crawl_id,))]
    links = [dict(r) for r in conn.execute(
        "SELECT source_url, target_url, anchor_text, nofollow, location FROM page_links "
        "WHERE crawl_id = ? AND internal = 1", (crawl_id,))]
    metrics, issues, summary = analyze(rows, links)
    conn.executemany("UPDATE crawl_pages SET link_metrics = ? WHERE crawl_id = ? AND normalized_url = ?",
                     [(store._dump(m), crawl_id, k) for k, m in metrics.items()])
    crawl = store.row_dict(conn.execute("SELECT site_issues, stats FROM crawls WHERE id = ?", (crawl_id,)).fetchone())
    codes = {i.code for i in issues} | {c for c, (cat, _, _) in SITE_RULES.items() if cat == "architecture"}
    kept = [i for i in (crawl["site_issues"] or []) if i["code"] not in codes]
    merged = sorted(kept + [i.model_dump() for i in issues], key=lambda i: SEVERITY_ORDER[i["severity"]])
    store.update(conn, "crawls", crawl_id, {"site_issues": merged,
                                            "stats": {**(crawl["stats"] or {}), "architecture": summary}})
    conn.commit()
