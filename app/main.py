import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.crawls import router as crawls_router
from app.api.seo import router as seo_router
from app.config import settings

log = logging.getLogger("seo")
app = FastAPI(title="SEO HTML Parser & Audit", version="1.0.0")

# JSON escaping can grow the body beyond the raw HTML size; allow headroom.
_MAX_BODY = settings.max_html_bytes * 2 + 64 * 1024


def _error(status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"success": False, "error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > _MAX_BODY:
        return _error(413, "PAYLOAD_TOO_LARGE", f"Request body exceeds {_MAX_BODY} bytes.")
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(x) for x in e["loc"][1:]) or "body", "message": e["msg"]}
               for e in exc.errors()]
    too_large = any("exceeds" in d["message"] for d in details)
    if too_large:
        return _error(413, "PAYLOAD_TOO_LARGE", "HTML payload too large.", details)
    return _error(422, "VALIDATION_ERROR", "Invalid request.", details)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    log.exception("Unhandled error")  # stack trace goes to logs only
    return _error(500, "INTERNAL_ERROR", "Internal server error.")


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(seo_router)
app.include_router(crawls_router)
