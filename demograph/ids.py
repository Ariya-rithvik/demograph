"""Stable identifiers for pages and facts.

A fact id points at one element on one page (url + css selector). A content hash
captures what that element says right now. Drift is the same fact id with a new
content hash.
"""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref)$", re.I)


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Canonical form used as a page's identity. Returns None for non-http(s) links."""
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return None
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not _TRACKING_PARAMS.match(k)))
    # Keep hash routes (#/settings) because SPAs use them as pages; drop plain anchors.
    fragment = parts.fragment if parts.fragment.startswith(("/", "!")) else ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, fragment))


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def page_id(url: str) -> str:
    return "p_" + _h(url)


def fact_id(url: str, selector: str) -> str:
    return "f_" + _h(f"{url}|{selector}")


def content_hash(*values: str | None) -> str:
    return _h("\x1f".join(v or "" for v in values))


def graph_name_for(url: str) -> str:
    """One FalkorDB graph per product: demograph_<host>_<port>."""
    netloc = urlsplit(url).netloc.lower()
    return "demograph_" + re.sub(r"[^a-z0-9]+", "_", netloc).strip("_")
