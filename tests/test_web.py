"""Tests for the web-link extractor (HTML->text + SSRF guard)."""

import pytest

from app.pipeline.extract import ExtractionError
from app.pipeline import web

SAMPLE_HTML = """
<html><head><title>  Photosynthesis Overview </title>
<style>.x{color:red}</style><script>var a=1;</script></head>
<body>
<nav>menu junk that should be skipped</nav>
<h1>Photosynthesis</h1>
<p>Photosynthesis is the process by which green plants convert sunlight into
chemical energy stored in glucose molecules.</p>
<p>Chlorophyll is the pigment responsible for absorbing light energy in the
thylakoid membranes of the chloroplast.</p>
<p>The Calvin cycle fixes carbon dioxide into sugar using ATP and NADPH produced
during the light-dependent reactions.</p>
<footer>copyright junk</footer>
</body></html>
"""


def test_html_to_text_strips_scripts_and_keeps_body():
    parser = web._HTMLToText()
    parser.feed(SAMPLE_HTML)
    text = parser.text()
    assert "Photosynthesis" in text
    assert "Chlorophyll" in text
    assert "var a=1" not in text  # script stripped
    assert "color:red" not in text  # style stripped
    assert parser.title.strip() == "Photosynthesis Overview"


def test_guard_rejects_non_http():
    with pytest.raises(ExtractionError):
        web._guard_url("ftp://example.com/file")
    with pytest.raises(ExtractionError):
        web._guard_url("file:///etc/passwd")


def test_guard_rejects_loopback():
    with pytest.raises(ExtractionError):
        web._guard_url("http://127.0.0.1:8000/secret")
    with pytest.raises(ExtractionError):
        web._guard_url("http://localhost/admin")


def test_fetch_url_text_success(monkeypatch):
    """Fetch path with the network mocked — no real HTTP."""
    import httpx

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        encoding = "utf-8"
        content = SAMPLE_HTML.encode()

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            return _Resp()

    monkeypatch.setattr(web, "_guard_url", lambda url: None)
    monkeypatch.setattr(httpx, "Client", _Client)
    title, text = web.fetch_url_text("https://example.com/photosynthesis")
    assert title == "Photosynthesis Overview"
    assert "Calvin cycle" in text


def test_fetch_url_text_too_little_content(monkeypatch):
    import httpx

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        encoding = "utf-8"
        content = b"<html><body><p>hi</p></body></html>"

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            return _Resp()

    monkeypatch.setattr(web, "_guard_url", lambda url: None)
    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(ExtractionError):
        web.fetch_url_text("https://example.com/thin")
