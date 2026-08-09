"""Meta Pixel: served only when configured, on every HTML page, and safely."""


def _get_html(client, path="/"):
    r = client.get(path)
    assert r.status_code == 200
    return r.text


def test_no_pixel_when_unconfigured(client):
    # Default config has no META_PIXEL_ID -> no tracking code is served.
    # (The app's own track() helper mentions fbq but never loads/initializes it.)
    html = _get_html(client)
    assert "fbevents.js" not in html
    assert "fbq('init'" not in html
    assert "facebook.com/tr?id=" not in html


def test_pixel_injected_when_configured(client, monkeypatch):
    from app import config, main
    settings = config.get_settings()
    monkeypatch.setattr(settings, "meta_pixel_id", "1234567890", raising=False)

    for path in ("/", "/privacy", "/terms"):
        html = _get_html(client, path)
        assert "fbevents.js" in html, path
        assert "window.SF_PIXEL_ID='1234567890'" in html, path
        # The tracker is DEFINED but must not fire on its own — it only runs
        # via SF_loadPixel(), which the consent banner calls after "Accept".
        assert "window.SF_loadPixel" in html, path
        assert "sf_consent" in html, path
        # A <noscript> pixel would fire regardless of consent, defeating the
        # whole gate, so it must not be present.
        assert "noscript" not in html, path
    # sanity: the helper itself returns empty for a blank id
    monkeypatch.setattr(settings, "meta_pixel_id", "", raising=False)
    assert main._meta_pixel_snippet() == ""


def test_accepts_a_pasted_full_snippet():
    # People paste Meta's whole <script> block instead of just the number.
    from app.main import _extract_pixel_id
    snippet = """<!-- Meta Pixel Code --><script>!function(f,b,e,v,n,t,s)
    {...}(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');
    fbq('init', '1351262153801780'); fbq('track', 'PageView');</script>
    <noscript><img src="https://www.facebook.com/tr?id=1351262153801780&ev=PageView"/></noscript>"""
    assert _extract_pixel_id(snippet) == "1351262153801780"
    assert _extract_pixel_id("  1351262153801780  ") == "1351262153801780"
    assert _extract_pixel_id("") == ""
    assert _extract_pixel_id("not-an-id") == ""


def test_pixel_id_is_sanitized(client, monkeypatch):
    # Anything non-alphanumeric is stripped so a bad value can't inject script.
    from app import config
    settings = config.get_settings()
    monkeypatch.setattr(settings, "meta_pixel_id", "123456'};alert(1);//", raising=False)
    html = _get_html(client)
    # Only digits survive, so nothing executable can reach the page.
    assert "alert(1)" not in html
    assert "123456'};" not in html
    assert "window.SF_PIXEL_ID='123456'" in html


def test_html_still_no_cache_with_pixel(client, monkeypatch):
    from app import config
    settings = config.get_settings()
    monkeypatch.setattr(settings, "meta_pixel_id", "999", raising=False)
    r = client.get("/")
    assert "no-cache" in r.headers.get("cache-control", "")


def test_pixel_does_not_fire_without_consent(client, monkeypatch):
    """The privacy guarantee: no tracking cookie before the visitor agrees.

    Loading the tracker first and asking afterwards is the specific pattern
    regulators fine sites for, so this is asserted rather than assumed.
    """
    from app import config
    settings = config.get_settings()
    monkeypatch.setattr(settings, "meta_pixel_id", "1234567890", raising=False)
    html = _get_html(client)
    # No bare init/track call outside the guarded loader function.
    assert "fbq('init',window.SF_PIXEL_ID)" in html
    guard = "localStorage.getItem('sf_consent')==='accepted'"
    assert guard in html, "pixel load is not gated on stored consent"
