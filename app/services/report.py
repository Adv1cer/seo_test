"""Render a ParsedPage + Audit as a human-readable Markdown report. Deterministic."""
from app.models.response import Audit, ParsedPage

_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵", "passed": "✅"}
_TITLE = {"critical": "Critical issues", "warning": "Warnings", "info": "Suggestions", "passed": "Passed checks"}


def _cell(value) -> str:
    text = "—" if value in (None, "", []) else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(page: ParsedPage, audit: Audit) -> str:
    lines = [
        f"# SEO Audit Report: {page.url}",
        "",
        f"**Score:** {audit.score}/100 ({audit.grade.replace('_', ' ')})  ",
        f"**Critical:** {audit.critical_count} · **Warnings:** {audit.warning_count} · "
        f"**Suggestions:** {audit.info_count} · **Passed:** {audit.passed_count}",
        "",
        "## Page overview",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Title | {_cell(page.title)} ({page.title_length} chars) |",
        f"| Meta description | {_cell(page.meta_description)} ({page.meta_description_length} chars) |",
        f"| Canonical | {_cell(page.canonical_absolute_url)} |",
        f"| Language | {_cell(page.language)} |",
        f"| H1 | {_cell(', '.join(page.h1))} |",
        f"| Hreflang | {_cell(', '.join(f'{h.lang} → {h.absolute_url}' for h in page.hreflang))} |",
        f"| Words (main content) | {page.main_content_word_count}"
        f"{' (approximate, Thai)' if page.word_count_is_approximate else ''} |",
        f"| Links (internal / external) | {page.internal_links_count} / {page.external_links_count} |",
        f"| Images (missing alt) | {page.images_total} ({page.images_without_alt}) |",
        f"| Structured data | {_cell(', '.join(page.structured_data_types))} |",
        f"| Robots | {_cell(page.meta_robots)} |",
    ]
    for severity in ("critical", "warning", "info"):
        issues = [i for i in audit.issues if i.severity == severity]
        if not issues:
            continue
        lines += ["", f"## {_ICON[severity]} {_TITLE[severity]}", ""]
        for i in issues:
            lines.append(f"- **{i.code}**: {i.message}")
            if i.value not in (None, "", []) and not isinstance(i.value, int):
                lines.append(f"  - Found: `{_cell(i.value)[:300]}`")
            lines.append(f"  - Fix: {i.recommendation}")
    passed = [i.category.replace("_", " ") for i in audit.issues if i.severity == "passed"]
    if passed:
        lines += ["", f"## {_ICON['passed']} {_TITLE['passed']}", "", ", ".join(passed)]
    return "\n".join(lines) + "\n"
