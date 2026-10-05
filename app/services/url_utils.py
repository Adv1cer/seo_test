"""URL resolution / classification helpers. Pure functions, no network access."""
import ipaddress
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

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
    path = parts.path or "/"
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
