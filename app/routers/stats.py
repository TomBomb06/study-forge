"""The traffic dashboard.

One page, one owner. Guarded by ADMIN_KEY, which is not set by default — with
no key configured the route does not exist at all and returns the ordinary 404
page, so nobody can discover that there is a dashboard here to attack.
"""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from .. import analytics
from ..config import get_settings
from ..db import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


def _authorised(key: str) -> bool:
    configured = (get_settings().admin_key or "").strip()
    if not configured:
        return False
    # compare_digest so a wrong key can't be found one character at a time.
    return secrets.compare_digest(key or "", configured)


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _page(d: dict, key: str) -> str:
    """A dashboard that answers one question: is anybody coming, and from where."""
    peak = max([r["views"] for r in d["daily"]] or [0]) or 1
    bars = "".join(
        f'<div class="b" title="{_esc(r["day"])}: {r["views"]} views">'
        f'<div class="f" style="height:{max(2, round(100 * r["views"] / peak))}%"></div>'
        f'<span>{_esc(r["day"][8:])}</span></div>'
        for r in d["daily"]
    )
    src_peak = max([r["views"] for r in d["sources"]] or [0]) or 1
    sources = "".join(
        f'<tr><td>{_esc(r["source"])}</td><td class="n">{r["views"]}</td>'
        f'<td class="bar"><i style="width:{round(100 * r["views"] / src_peak)}%"></i></td></tr>'
        for r in d["sources"]
    ) or '<tr><td colspan="3" class="empty">No visits recorded yet.</td></tr>'
    pages = "".join(
        f'<tr><td>{_esc(r["path"])}</td><td class="n">{r["views"]}</td></tr>'
        for r in d["pages"]
    ) or '<tr><td colspan="2" class="empty">Nothing yet.</td></tr>'

    rate = (f'{d["signup_rate"]}%' if d["signup_rate"] is not None else "—")
    n = d["window_days"]

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
<title>StudyForge — traffic</title><style>
:root{{--bg:#070d1a;--surface:#0e1524;--border:#1d2c42;--text:#eef2f8;--muted:#9aa3b2;
  --blue:#58a6ff;--green:#3ecf8e;--gold:#ffd166;--violet:#a78bfa}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:26px 18px 60px}}
.wrap{{max-width:840px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 2px}} .sub{{color:var(--muted);font-size:13px;margin-bottom:22px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:26px}}
.c{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:15px 16px}}
.c .k{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
.c .v{{font-size:30px;font-weight:800;letter-spacing:-.02em;margin-top:5px}}
.c .s{{color:var(--muted);font-size:12px;margin-top:3px}}
.c.v1 .v{{color:var(--blue)}} .c.v2 .v{{color:var(--green)}}
.c.v3 .v{{color:var(--gold)}} .c.v4 .v{{color:var(--violet)}}
h2{{font-size:15px;margin:26px 0 10px}}
.chart{{display:flex;align-items:flex-end;gap:4px;height:150px;background:var(--surface);
  border:1px solid var(--border);border-radius:14px;padding:14px 12px 4px}}
.b{{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%}}
.b .f{{width:100%;border-radius:4px 4px 0 0;background:linear-gradient(180deg,var(--blue),#2f6fed);min-height:2px}}
.b span{{font-size:10px;color:var(--muted);margin-top:5px}}
table{{width:100%;border-collapse:collapse;background:var(--surface);
  border:1px solid var(--border);border-radius:14px;overflow:hidden}}
td,th{{padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);font-size:14px}}
tr:last-child td{{border-bottom:0}}
td.n{{text-align:right;font-weight:700;width:70px}}
td.bar{{width:45%}} td.bar i{{display:block;height:8px;border-radius:99px;background:var(--green)}}
td.empty{{color:var(--muted);text-align:center}}
.note{{color:var(--muted);font-size:12.5px;margin-top:22px;line-height:1.6;
  border-left:2px solid var(--border);padding-left:12px}}
a{{color:var(--blue)}}
</style></head><body><div class="wrap">
<h1>StudyForge traffic</h1>
<div class="sub">Last {n} days · counted on the server, so the cookie banner doesn't hide anyone</div>

<div class="cards">
  <div class="c v1"><div class="k">Page views</div><div class="v">{d["views_total"]}</div>
    <div class="s">last {n} days</div></div>
  <div class="c v2"><div class="k">Signups</div><div class="v">{d["users_window"]}</div>
    <div class="s">{d["users_today"]} today · {d["users_total"]} all time</div></div>
  <div class="c v3"><div class="k">Visit → signup</div><div class="v">{rate}</div>
    <div class="s">of views in the window</div></div>
  <div class="c v4"><div class="k">Study sets</div><div class="v">{d["sets_window"]}</div>
    <div class="s">{d["sets_total"]} all time</div></div>
</div>

<h2>Views per day</h2>
<div class="chart">{bars}</div>

<h2>Where they came from</h2>
<table>{sources}</table>

<h2>Which page</h2>
<table>{pages}</table>

<div class="note">
  <b>"direct" means no referrer</b> — someone typed the address, or came from an app
  that strips it. TikTok and Instagram both open bio links in their own browser and
  usually send nothing, so untagged social traffic lands in "direct".
  Tag your links to separate them: <code>forge.study/?utm_source=tiktok</code> in the
  TikTok bio, <code>?utm_source=instagram</code> in the Instagram one.<br><br>
  These are page <i>views</i>, not unique people — one person reloading counts twice.
  Telling them apart would mean fingerprinting visitors who declined to be tracked,
  which isn't worth it.<br><br>
  <a href="/admin/stats?key={_esc(key)}&amp;days=7">7 days</a> ·
  <a href="/admin/stats?key={_esc(key)}&amp;days=14">14 days</a> ·
  <a href="/admin/stats?key={_esc(key)}&amp;days=30">30 days</a> ·
  <a href="/admin/stats?key={_esc(key)}&amp;days={n}&amp;format=json">raw JSON</a>
</div>
</div></body></html>"""


@router.get("/stats", include_in_schema=False)
def stats(
    request: Request,
    key: str = Query(""),
    days: int = Query(14, ge=1, le=90),
    format: str = Query("html"),
    db: Session = Depends(get_db),
):
    if not _authorised(key):
        # 404, not 403: an unauthenticated visitor should not learn this exists.
        raise HTTPException(status_code=404, detail="Not found")
    data = analytics.summary(db, days)
    if format == "json":
        return JSONResponse(data)
    return HTMLResponse(_page(data, key),
                        headers={"Cache-Control": "no-store",
                                 "X-Robots-Tag": "noindex, nofollow"})
