"""Transactional email.

Deliberately provider-agnostic and fail-soft:

  * `console`  — development. Prints the email (including the reset link) to
                 the server log so you can test the whole flow with no account
                 anywhere and no cost.
  * `resend`   — production. One HTTPS call, no SMTP configuration, generous
                 free tier. https://resend.com
  * `smtp`     — if you'd rather use Gmail/Fastmail/anything else.

Sending must never raise into a request handler. If mail is down, a password
reset should fail quietly and identically to a request for an address that
doesn't exist — otherwise the error message itself tells an attacker which
emails are real.
"""

import hashlib
import re
import smtplib
import ssl
from email.message import EmailMessage

from .config import get_settings


class MailError(Exception):
    """Internal — never surfaced to the user."""


# Matches a reset link so its token can be stripped from any log line.
_LINK_RE = re.compile(r"(https?://\S*?[#?]reset=)\S+")


def _redact(value: str) -> str:
    """A stable, non-reversible stand-in for an address in a log line."""
    return "u:" + hashlib.sha256((value or "").encode()).hexdigest()[:10]


def _send_console(to: str, subject: str, text: str, html: str) -> None:
    """Print the email instead of sending it — DEVELOPMENT ONLY.

    On a real deploy this writes live password-reset links into the platform
    log, where anyone who can read logs (support, CI, a log aggregator) can use
    one to take over an account. verify_production_config() now refuses to boot
    a production deploy with EMAIL_PROVIDER=console, and this redacts the link
    anyway as a second line of defence.
    """
    from .config import looks_like_production

    safe = not looks_like_production(get_settings())
    print("\n" + "=" * 68)
    print(f"[email:console] To: {to if safe else _redact(to)}")
    print(f"[email:console] Subject: {subject}")
    print("-" * 68)
    print(text if safe else _LINK_RE.sub(lambda m: m.group(1) + "<redacted>", text))
    print("=" * 68 + "\n", flush=True)


def _send_resend(to: str, subject: str, text: str, html: str) -> None:
    import httpx

    s = get_settings()
    if not s.resend_api_key:
        raise MailError("RESEND_API_KEY is not set")
    r = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {s.resend_api_key}"},
        json={"from": s.email_from, "to": [to], "subject": subject,
              "text": text, "html": html},
        timeout=15,
    )
    if r.status_code >= 300:
        raise MailError(f"resend returned {r.status_code}: {r.text[:200]}")


def _send_smtp(to: str, subject: str, text: str, html: str) -> None:
    s = get_settings()
    if not (s.smtp_host and s.smtp_user and s.smtp_password):
        raise MailError("SMTP settings incomplete")
    msg = EmailMessage()
    msg["From"] = s.email_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as server:
        server.starttls(context=ctx)
        server.login(s.smtp_user, s.smtp_password)
        server.send_message(msg)


_SENDERS = {"console": _send_console, "resend": _send_resend, "smtp": _send_smtp}


def send(to: str, subject: str, text: str, html: str) -> bool:
    """Send an email. Returns True on success, False on any failure.

    Never raises: callers are request handlers where an exception would leak
    whether the address exists.
    """
    provider = (get_settings().email_provider or "console").lower()
    fn = _SENDERS.get(provider, _send_console)
    try:
        fn(to, subject, text, html)
        return True
    except Exception as e:  # noqa: BLE001 - deliberately swallowing
        # Hashed, not the address: this line fires on every delivery failure,
        # so the raw form turned the log into a slowly-accumulating list of
        # every user's email.
        print(f"[email:error] provider={provider} to={_redact(to)}: {e}", flush=True)
        return False


# ------------------------------------------------------------ templates

def _shell(title: str, body_html: str) -> str:
    return f"""<!doctype html><html><body style="margin:0;background:#f7f8fb;
 font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1d29">
<div style="max-width:520px;margin:0 auto;padding:32px 22px">
  <div style="font-size:22px;font-weight:800;letter-spacing:-.4px;margin-bottom:20px">
    Study<span style="color:#5b5bd6">Forge</span></div>
  <div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:26px">
    <h1 style="font-size:20px;margin:0 0 14px">{title}</h1>
    {body_html}
  </div>
  <div style="color:#6b7280;font-size:12.5px;text-align:center;margin-top:22px">
    StudyForge · <a href="https://forge.study" style="color:#5b5bd6">forge.study</a>
  </div>
</div></body></html>"""


def password_reset(to: str, link: str, minutes: int) -> bool:
    subject = "Reset your StudyForge password"
    text = (
        "Someone asked to reset the password for your StudyForge account.\n\n"
        f"Open this link to choose a new password:\n{link}\n\n"
        f"The link expires in {minutes} minutes and can only be used once.\n\n"
        "If this wasn't you, you can ignore this email — your password will "
        "not change and your account is safe.\n"
    )
    html = _shell(
        "Reset your password",
        f"""<p>Someone asked to reset the password for your StudyForge account.</p>
        <p style="margin:22px 0"><a href="{link}"
           style="background:linear-gradient(135deg,#2f6fed,#58a6ff);color:#fff;
           text-decoration:none;font-weight:700;padding:13px 22px;border-radius:12px;
           display:inline-block">Choose a new password</a></p>
        <p style="color:#6b7280;font-size:13.5px">This link expires in {minutes} minutes
        and can only be used once.</p>
        <p style="color:#6b7280;font-size:13.5px">If this wasn't you, ignore this email —
        your password won't change and your account is safe.</p>
        <p style="color:#9aa0ac;font-size:12px;word-break:break-all;margin-top:18px">
        If the button doesn't work, paste this into your browser:<br>{link}</p>""",
    )
    return send(to, subject, text, html)


def password_changed(to: str) -> bool:
    """Sent after a successful reset.

    This is a security control, not a courtesy: if an attacker resets someone's
    password, this is the message that tells the real owner it happened.
    """
    subject = "Your StudyForge password was changed"
    text = (
        "Your StudyForge password was just changed.\n\n"
        "If you did this, no action is needed.\n\n"
        "If you did NOT do this, reply to this email immediately — someone may "
        "have access to your account.\n"
    )
    html = _shell(
        "Your password was changed",
        """<p>Your StudyForge password was just changed.</p>
        <p>If this was you, there's nothing to do.</p>
        <p style="color:#b42318"><b>If this wasn't you</b>, reply to this email
        immediately — someone may have access to your account.</p>""",
    )
    return send(to, subject, text, html)
