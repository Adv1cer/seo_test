"""URL resolution / classification helpers. Pure functions, no network access."""
import ipaddress
import re
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

WEB_SCHEMES = ("http", "https")


def safe_split(url: str):
    """urlsplit that returns None instead of raising on malformed input (e.g. 'http://[::1')."""
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError on bad ports
        return parts
    except ValueError:
        return None


def resolve(base_url: str, href: str | None) -> str | None:
    """Resolve href against base_url. Handles relative, //host, absolute. None if unresolvable."""
    if href is None:
        return None
    href = href.strip()
    if not href or safe_split(href) is None:
        return None
    try:
        return urljoin(base_url, href)
    except ValueError:
        return None


def host_of(url: str | None) -> str:
    parts = safe_split(url or "")
    return (parts.hostname or "").lower().rstrip(".") if parts else ""


def site_key(host: str) -> str:
    """Host used for internal/external comparison: lowercase, leading 'www.' ignored.
    Subdomains (blog.example.com vs example.com) are treated as different sites."""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_site(url_a: str | None, url_b: str | None) -> bool:
    a, b = host_of(url_a), host_of(url_b)
    return bool(a) and site_key(a) == site_key(b)


def is_private_host(host: str) -> bool:
    """True for private / loopback / link-local / reserved IPs and localhost names."""
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


def is_web_url(url: str | None) -> bool:
    parts = safe_split(url or "")
    return bool(parts and parts.scheme.lower() in WEB_SCHEMES and parts.hostname)


_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PCT = re.compile(r"%([0-9A-Fa-f]{2})")


def _decode_unreserved(path: str) -> str:
    """RFC 3986 §6.2.2: %77%70 == wp. Decode escaped unreserved chars, uppercase the other escapes, and
    percent-encode raw non-ASCII (IRI -> URI), so /คณะ and /%e0%b8%84%e0%b8%93%e0%b8%b0 are one URL."""
    def sub(m):
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()
    path = _PCT.sub(sub, path)
    return quote(path, safe="/%:@!$&'()*+,;=~") if not path.isascii() else path


ASSET_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "ico", "bmp", "tif", "tiff", "heic",
                    "pdf", "zip", "rar", "7z", "gz", "mp3", "mp4", "m4a", "wav", "webm", "mov", "avi",
                    "css", "js", "json", "xml", "txt", "csv", "woff", "woff2", "ttf", "otf", "eot",
                    "doc", "docx", "xls", "xlsx", "ppt", "pptx"}


_ASSET_TYPES = ("image/", "font/", "video/", "audio/", "text/css", "javascript", "application/font",
                "application/vnd.ms-fontobject")


def resource_type(url: str | None, content_type: str | None, status_code: int | None) -> str:
    """document | asset | non_html. Content-Type is authoritative for successful responses (< 400): text/html
    is a document even at /photo.jpg, image/* at /img/123 is an asset. Error responses usually carry the
    server's HTML error page, whose Content-Type says nothing about the requested resource, so they (and
    responses without Content-Type) fall back to the URL extension."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and (status_code or 0) < 400:
        if "html" in ct:
            return "document"
        if ct.startswith(_ASSET_TYPES):
            return "asset"
        return "non_html"  # application/pdf, application/zip, json, xml, plain text...
    return "asset" if is_asset_url(url) else "document"


def is_asset_url(url: str | None) -> bool:
    """True when the URL path names a file/media resource (image, PDF, script...) rather than a page.
    Judged by extension only, so it also holds for assets that 404 with an HTML error page."""
    parts = safe_split(url or "")
    last = (parts.path if parts else "").rsplit("/", 1)[-1]
    return "." in last and _decode_unreserved(last).rsplit(".", 1)[-1].lower() in ASSET_EXTENSIONS


_LANG_SEGMENT = re.compile(r"^/([a-z]{2}(?:[-_][a-z]{2})?)(?:/|$)", re.I)


def language_segment(url: str | None) -> str | None:
    """'en' for https://x/en/news, 'th-th' for /th-TH/; None when the first path segment is not a language."""
    parts = safe_split(url or "")
    m = _LANG_SEGMENT.match(parts.path if parts else "")
    return m.group(1).lower().replace("_", "-") if m else None


def normalize(url: str) -> str:
    """Canonical form for de-duplication: lowercase scheme/host, drop default port,
    drop fragment, strip trailing slash (except root). Query string is kept."""
    parts = safe_split(url)
    if parts is None:
        return url
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = _decode_unreserved(parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((scheme, host, path, parts.query, ""))


def classify_link(page_url: str, href: str | None) -> tuple[str, str | None]:
    """Return (type, absolute_url). type: internal|external|anchor|mailto|tel|javascript|invalid."""
    raw = (href or "").strip()
    if not raw:
        return "invalid", None
    lower = raw.lower()
    if lower.startswith("#"):
        return "anchor", resolve(page_url, raw)
    for scheme in ("mailto", "tel", "javascript"):
        if lower.startswith(scheme + ":"):
            return scheme, raw
    absolute = resolve(page_url, raw)
    if not is_web_url(absolute):
        return "invalid", absolute
    # Same document + fragment only → anchor
    if urlsplit(absolute).fragment and normalize(absolute) == normalize(page_url):
        return "anchor", absolute
    return ("internal" if same_site(absolute, page_url) else "external"), absolute


TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "dclid", "yclid", "mc_cid", "mc_eid", "_ga", "_gl",
                   "igshid", "ref_src", "spm"}


def crawl_key(url: str) -> str:
    """normalize() plus crawl de-duplication: drop tracking params (utm_* etc.), drop
    duplicate keys (first wins), sort the query so ?a=1&b=2 == ?b=2&a=1."""
    norm = normalize(url)
    parts = urlsplit(norm)
    if not parts.query:
        return norm
    seen, params = set(), []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS or k in seen:
            continue
        seen.add(k)
        params.append((k, v))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(params)), ""))
