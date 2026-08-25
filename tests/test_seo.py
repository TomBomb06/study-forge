"""SEO / professionalism guarantees.

These are cheap to break silently (a stray edit drops a meta tag and nobody
notices for months), so they're asserted.
"""


def test_robots_txt_served(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Sitemap: https://forge.study/sitemap.xml" in r.text
    assert "Disallow: /app/" in r.text


def test_sitemap_served_and_valid(client):
    import xml.etree.ElementTree as ET
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    locs = [e.text for e in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert "https://forge.study/" in locs
    assert "https://forge.study/privacy" in locs


def test_custom_404_page(client):
    r = client.get("/this-page-does-not-exist")
    assert r.status_code == 404
    assert "404" in r.text
    assert "Back to StudyForge" in r.text
    # must not be indexed
    assert 'name="robots" content="noindex' in r.text


def test_api_404_stays_json(client):
    """An API client hitting a bad path should get JSON, not an HTML page."""
    r = client.get("/math/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_every_page_has_a_unique_title_and_description(client):
    import re
    seen_titles, seen_descs = set(), set()
    for path in ("/", "/privacy", "/terms"):
        html = client.get(path).text
        title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
        desc = re.search(r'<meta name="description" content="(.*?)"', html, re.S).group(1)
        assert title and desc
        assert title not in seen_titles, f"duplicate title on {path}"
        assert desc not in seen_descs, f"duplicate description on {path}"
        seen_titles.add(title)
        seen_descs.add(desc)


def test_social_share_image_on_every_page(client):
    for path in ("/", "/privacy", "/terms"):
        html = client.get(path).text
        assert 'og:image" content="https://forge.study/og.png' in html, path
        # alt text matters for screen readers and for the "alt text" checklist
        assert 'og:image:alt"' in html, path


def test_canonical_urls_present(client):
    for path, expect in (("/", "https://forge.study/"),
                         ("/privacy", "https://forge.study/privacy"),
                         ("/terms", "https://forge.study/terms")):
        assert f'rel="canonical" href="{expect}"' in client.get(path).text, path


def test_breadcrumbs_on_subpages(client):
    for path in ("/privacy", "/terms"):
        html = client.get(path).text
        assert 'aria-label="Breadcrumb"' in html, path
        assert "BreadcrumbList" in html, path


def test_landing_has_faq_with_structured_data(client):
    html = client.get("/").text
    assert 'id="faq"' in html
    assert "FAQPage" in html
    # five questions, as asked for
    assert html.count('"@type":"Question"') == 5


def test_landing_structured_data_is_valid_json(client):
    import json, re
    html = client.get("/").text
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        json.loads(block)


def test_internal_links_between_pages(client):
    """Every page should link onward — orphan pages are bad for users and SEO."""
    for path in ("/privacy", "/terms"):
        html = client.get(path).text
        assert 'href="/"' in html, path
        assert 'href="/privacy"' in html and 'href="/terms"' in html, path
    home = client.get("/").text
    for anchor in ('href="#features"', 'href="#how"', 'href="#pricing"', 'href="#faq"'):
        assert anchor in home, anchor


def test_sticky_mobile_cta_present(client):
    html = client.get("/").text
    assert 'id="sticky-cta"' in html
    assert "wireStickyCta" in html


def test_response_time_promise_is_stated(client):
    assert "one business day" in client.get("/").text


def test_analytics_is_consent_gated(client, monkeypatch):
    """GA must not set cookies before the visitor accepts — same rule as the Pixel."""
    from app import config
    settings = config.get_settings()
    monkeypatch.setattr(settings, "ga_measurement_id", "G-ABC123XYZ", raising=False)
    html = client.get("/").text
    assert "window.SF_loadGA" in html
    assert "localStorage.getItem('sf_consent')==='accepted'" in html
    # no bare gtag config outside the guarded loader
    assert "googletagmanager.com/gtag/js?id='+window.SF_GA_ID" in html


def test_no_analytics_code_when_unconfigured(client):
    from app import config, main
    settings = config.get_settings()
    assert main._ga_snippet() == "" or settings.ga_measurement_id
