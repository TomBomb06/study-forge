"""Server-side visitor counting.

Two things matter here and both are easy to get wrong later:

  * The counter must never break a page. Every test that exercises a failure
    asserts the page still renders.
  * The dashboard must be invisible without the key — a 404, not a 403, so
    nobody learns it is there.
"""

import pytest

from app import analytics
from app.models import PageHit


# ---------------------------------------------------------------- classify

@pytest.mark.parametrize("referer,expected", [
    ("https://www.tiktok.com/@someone", "tiktok"),
    ("https://vm.tiktok.com/abc", "tiktok"),
    ("https://l.instagram.com/?u=x", "instagram"),
    ("https://www.instagram.com/", "instagram"),
    ("https://m.facebook.com/", "facebook"),
    ("https://www.google.com/search?q=x", "google"),
    ("https://www.reddit.com/r/x", "reddit"),
    ("", "direct"),
    (None, "direct"),
])
def test_classify_reads_the_referrer(referer, expected):
    assert analytics.classify(referer) == expected


def test_utm_source_beats_the_referrer():
    """The tagged link is the only signal that survives an in-app browser, so
    it has to win outright."""
    assert analytics.classify("https://www.google.com/",
                              {"utm_source": "tiktok"}) == "tiktok"


def test_ad_click_ids_are_recognised_without_a_referrer():
    assert analytics.classify("", {"fbclid": "abc"}) == "meta-ads"
    assert analytics.classify("", {"gclid": "abc"}) == "google-ads"
    assert analytics.classify("", {"ttclid": "abc"}) == "tiktok-ads"


def test_utm_source_is_sanitised():
    """It arrives from the URL bar, so it is attacker-controlled and lands in
    an HTML table."""
    out = analytics.classify("", {"utm_source": '<script>alert(1)</script>'})
    assert "<" not in out and ">" not in out
    assert len(out) <= analytics.MAX_SOURCE_LEN


def test_a_very_long_utm_source_cannot_overflow_the_column():
    assert len(analytics.classify("", {"utm_source": "x" * 500})) <= analytics.MAX_SOURCE_LEN


def test_our_own_pages_are_not_a_traffic_source():
    assert analytics.classify("https://forge.study/privacy") == "internal"


def test_an_unknown_referrer_keeps_its_hostname():
    assert analytics.classify("https://www.example.org/page") == "example.org"


# ------------------------------------------------------------- should_count

def test_crawlers_are_not_visitors():
    """A week of Googlebot must not look like a week of students."""
    for ua in ["Googlebot/2.1", "facebookexternalhit/1.1", "python-requests/2.31",
               "curl/8.4.0", "AhrefsBot", "HeadlessChrome/120"]:
        assert analytics.should_count("/", ua) is False


def test_a_real_browser_counts():
    ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
    assert analytics.should_count("/", ua) is True


def test_assets_and_api_calls_are_not_page_views():
    assert analytics.should_count("/og.png", "Mozilla/5.0") is False
    assert analytics.should_count("/auth/login", "Mozilla/5.0") is False


# ------------------------------------------------------------------- record

def _db():
    from app.db import SessionLocal
    return SessionLocal()


def _views(db, source):
    rows = db.query(PageHit).filter(PageHit.source == source).all()
    return sum(r.views or 0 for r in rows)


def test_record_counts_and_accumulates(client):
    db = _db()
    try:
        for _ in range(3):
            analytics.record(db, "/", "https://www.tiktok.com/@x", "Mozilla/5.0")
        assert _views(db, "tiktok") == 3
    finally:
        db.close()


def test_record_separates_sources(client):
    db = _db()
    try:
        analytics.record(db, "/", "https://www.instagram.com/", "Mozilla/5.0")
        analytics.record(db, "/", "", "Mozilla/5.0")
        assert _views(db, "instagram") >= 1
        assert _views(db, "direct") >= 1
    finally:
        db.close()


def test_record_never_raises_on_a_broken_database(client):
    """The whole point of the try/except in record(). If this regresses, a
    database hiccup takes the landing page down with it."""
    class Boom:
        def query(self, *a, **k): raise RuntimeError("db is on fire")
        def rollback(self): raise RuntimeError("still on fire")
    analytics.record(Boom(), "/", "", "Mozilla/5.0")   # must not raise


# ---------------------------------------------------------------- the page

def test_serving_the_landing_page_records_a_visit(client):
    db = _db()
    try:
        before = _views(db, "tiktok")
    finally:
        db.close()

    r = client.get("/?utm_source=tiktok",
                   headers={"user-agent": "Mozilla/5.0 (iPhone) Safari/604.1"})
    assert r.status_code == 200

    db = _db()
    try:
        assert _views(db, "tiktok") == before + 1
    finally:
        db.close()


def test_the_page_still_serves_when_counting_fails(client, monkeypatch):
    """Load-bearing. The site must not depend on the analytics table."""
    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(analytics, "record", boom)
    assert client.get("/").status_code == 200


# ------------------------------------------------------------- the dashboard

def test_dashboard_is_invisible_without_a_key(client):
    """404, not 403 — an unauthenticated visitor should not learn it exists."""
    assert client.get("/admin/stats").status_code == 404
    assert client.get("/admin/stats?key=wrong").status_code == 404


def test_dashboard_stays_off_when_no_admin_key_is_configured(client, monkeypatch):
    """The default. A blank key must never mean 'let everyone in'."""
    from app.routers import stats as stats_router
    s = stats_router.get_settings()
    monkeypatch.setattr(s, "admin_key", "", raising=False)
    assert client.get("/admin/stats?key=").status_code == 404
    assert client.get("/admin/stats").status_code == 404


def _with_key(monkeypatch, key="test-admin-key"):
    from app.routers import stats as stats_router
    monkeypatch.setattr(stats_router.get_settings(), "admin_key", key, raising=False)
    return key


def test_dashboard_renders_with_the_right_key(client, monkeypatch):
    key = _with_key(monkeypatch)
    r = client.get(f"/admin/stats?key={key}")
    assert r.status_code == 200
    assert "StudyForge traffic" in r.text


def test_dashboard_is_not_indexable(client, monkeypatch):
    key = _with_key(monkeypatch)
    r = client.get(f"/admin/stats?key={key}")
    assert "noindex" in r.headers.get("x-robots-tag", "").lower()
    assert "noindex" in r.text


def test_dashboard_json_reports_the_real_numbers(client, monkeypatch):
    key = _with_key(monkeypatch)
    db = _db()
    try:
        analytics.record(db, "/", "https://www.tiktok.com/@x", "Mozilla/5.0")
    finally:
        db.close()

    d = client.get(f"/admin/stats?key={key}&format=json").json()
    assert d["views_total"] >= 1
    assert any(s["source"] == "tiktok" for s in d["sources"])
    assert "users_total" in d and "sets_total" in d
    assert len(d["daily"]) == d["window_days"]


def test_quiet_days_appear_as_zeroes_not_gaps(client, monkeypatch):
    """A day with no traffic is the most informative point on the chart."""
    key = _with_key(monkeypatch)
    d = client.get(f"/admin/stats?key={key}&days=14&format=json").json()
    assert len(d["daily"]) == 14
    assert all("views" in row for row in d["daily"])


def test_a_hostile_utm_source_cannot_inject_html(client, monkeypatch):
    """It goes straight from the URL into the dashboard table."""
    key = _with_key(monkeypatch)
    client.get("/?utm_source=<img src=x onerror=alert(1)>",
               headers={"user-agent": "Mozilla/5.0 (iPhone) Safari/604.1"})
    body = client.get(f"/admin/stats?key={key}").text
    assert "<img src=x" not in body
    assert "onerror=alert" not in body
