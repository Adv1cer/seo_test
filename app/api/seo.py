import json
from typing import Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from app.models.request import SeoRequest
from app.models.response import AuditData, AuditResponse, PageSummary, ParseResponse
from app.services.html_parser import parse_html
from app.services.report import render_markdown
from app.services.seo_auditor import audit_page

router = APIRouter(prefix="/api/seo", tags=["seo"])


def _body_error(msg: str) -> RequestValidationError:
    return RequestValidationError([{"loc": ("body",), "msg": msg, "type": "body_invalid"}])


async def read_seo_request(request: Request) -> SeoRequest:
    """Lenient body reader for workflow tools: accepts a JSON object, a JSON object
    double-encoded as a JSON string, urlencoded form data, or raw HTML with ?url=...
    (any Content-Type)."""
    raw = await request.body()
    query_url = request.query_params.get("url")
    if query_url:  # ?url=... → body is the raw HTML itself (no JSON escaping needed)
        data = {"url": query_url, "html": raw.decode("utf-8", "replace")}
    elif "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
        data = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
    else:
        try:
            data = json.loads(raw) if raw.strip() else None
            if isinstance(data, str):  # body was sent as a JSON string containing JSON
                data = json.loads(data)
        except ValueError as exc:
            raise _body_error(f"Body is not valid JSON ({exc.msg} at char {exc.pos}). "
                              "Either JSON-escape html, or send raw HTML as the body with ?url=<page url>.")
    if not isinstance(data, dict):
        raise _body_error(f'Expected a JSON object {{"url": ..., "html": ...}}; got {type(data).__name__}.')
    try:
        return SeoRequest.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError([{**e, "loc": ("body", *e["loc"])} for e in exc.errors()])


@router.post("/parse", response_model=ParseResponse)
def parse(req: SeoRequest = Depends(read_seo_request)) -> ParseResponse:
    return ParseResponse(data=parse_html(req.url, req.html))


def _audit_data(page) -> AuditData:
    summary = PageSummary(**page.model_dump(include=set(PageSummary.model_fields)))
    return AuditData(page=summary, audit=audit_page(page))


@router.post("/audit", response_model=AuditResponse)
def audit(req: SeoRequest = Depends(read_seo_request)) -> AuditResponse:
    return AuditResponse(data=_audit_data(parse_html(req.url, req.html)))


@router.post("/report")
def report(req: SeoRequest = Depends(read_seo_request), format: Literal["json", "markdown"] = "json"):
    """Audit + human-readable Markdown report. format=markdown returns the raw report text."""
    page = parse_html(req.url, req.html)
    data = _audit_data(page)
    md = render_markdown(page, data.audit)
    if format == "markdown":
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")
    return {"success": True, "data": {**data.model_dump(), "report_markdown": md}}
