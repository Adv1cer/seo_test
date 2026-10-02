from typing import Literal

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.models.request import SeoRequest
from app.models.response import AuditData, AuditResponse, PageSummary, ParseResponse
from app.services.html_parser import parse_html
from app.services.report import render_markdown
from app.services.seo_auditor import audit_page

router = APIRouter(prefix="/api/seo", tags=["seo"])


@router.post("/parse", response_model=ParseResponse)
def parse(req: SeoRequest) -> ParseResponse:
    return ParseResponse(data=parse_html(req.url, req.html))


def _audit_data(page) -> AuditData:
    summary = PageSummary(**page.model_dump(include=set(PageSummary.model_fields)))
    return AuditData(page=summary, audit=audit_page(page))


@router.post("/audit", response_model=AuditResponse)
def audit(req: SeoRequest) -> AuditResponse:
    return AuditResponse(data=_audit_data(parse_html(req.url, req.html)))


@router.post("/report")
def report(req: SeoRequest, format: Literal["json", "markdown"] = "json"):
    """Audit + human-readable Markdown report. format=markdown returns the raw report text."""
    page = parse_html(req.url, req.html)
    data = _audit_data(page)
    md = render_markdown(page, data.audit)
    if format == "markdown":
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")
    return {"success": True, "data": {**data.model_dump(), "report_markdown": md}}
