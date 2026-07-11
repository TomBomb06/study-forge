"""Fetch a web page and extract readable text.

Used by the "paste a web link" input. Generic article extraction — good
for most text-heavy pages. JavaScript-heavy pages (and YouTube, whose
transcript needs a dedicated API) are a Phase 2 concern; if a page yields
too little text, the caller surfaces the normal "not enough text" error.
"""

import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from .extract import ExtractionError

_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "form"}
_BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}
_MAX_BYTES = 5 * 1024 * 1024  # don't slurp huge pages


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        text = data.strip()
        if text:
            self._chunks.append(text + " ")

    def text(self) -> str:
        joined = "".join(self._chunks)
        # Collapse runs of blank lines/spaces.
        lines = [ln.strip() for ln in joined.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _guard_url(url: str) -> None:
    """Reject non-http(s) and private/loopback targets (basic SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ExtractionError("Please enter a web link starting with http:// or https://")
    host = parsed.hostname
    if not host:
        raise ExtractionError("That doesn't look like a valid web link.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ExtractionError("Couldn't reach that link — check the address and try again.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ExtractionError("That link points to a private address and can't be fetched.")


def fetch_url_text(url: str) -> tuple[str, str]:
    """Return (title, text) for a web page. Raises ExtractionError on failure."""
    url = url.strip()
    _guard_url(url)
    headers = {"User-Agent": "StudyForgeBot/1.0 (+study-kit generator)"}
    try:
        with httpx.Client(follow_redirects=True, timeout=20.0, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                raise ExtractionError(
                    "That link isn't a readable web page (it looks like a file or media)."
                )
            raw = resp.content[:_MAX_BYTES]
            html = raw.decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPStatusError as e:
        raise ExtractionError(
            f"The page couldn't be loaded (server said {e.response.status_code})."
        )
    except httpx.HTTPError:
        raise ExtractionError("Couldn't load that link — it may be down or blocking requests.")

    parser = _HTMLToText()
    parser.feed(html)
    title = " ".join(parser.title.split()) or urlparse(url).netloc
    text = parser.text()
    if len(text.strip()) < 200:
        raise ExtractionError(
            "There wasn't enough readable text on that page. Some sites (and video "
            "pages like YouTube) load their content with JavaScript, which isn't "
            "supported yet — try pasting the text directly instead."
        )
    return title[:255], text
