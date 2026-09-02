import os
from functools import lru_cache
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_KEY = "dev-only-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./studyforge.db"
    # The JWT signing key. If this is ever left at the default in production,
    # anyone who reads this file (it's a public repo) can forge a login token
    # for ANY user — no password needed. `verify_production_config()` below
    # refuses to boot in that state rather than run silently insecure.
    secret_key: str = DEV_SECRET_KEY
    # Comma-separated list of allowed browser origins, or "*" for any.
    allowed_origins: str = "*"
    # How many proxies sit in front of this app (Railway alone = 1; add one for
    # Cloudflare or a load balancer). Used to pick the trustworthy entry out of
    # X-Forwarded-For — see ratelimit.client_ip.
    trusted_proxy_hops: int = 1
    access_token_expire_minutes: int = 10080  # 7 days
    storage_dir: str = "./storage"
    max_upload_mb: int = 25
    max_media_mb: int = 200  # audio/video lectures are bigger

    generator: str = "mock"  # "mock" | "claude"
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5"          # paid plans: best quality
    claude_model_free: str = "claude-haiku-4-5-20251001"  # free plan: fast + cheap

    # Premium AI video. "stub" = metering works, no real video yet (no cost).
    # "higgsfield" = call the real Cloud API (requires a key + spends money).
    video_provider: str = "stub"  # "stub" | "openai" | "higgsfield"

    # Higgsfield Cloud API (get a key at cloud.higgsfield.ai). Credentials are
    # the "KEY_ID:KEY_SECRET" pair. Endpoint/model come from your dashboard —
    # different video models live at different endpoints.
    higgsfield_credentials: str = ""  # "KEY_ID:KEY_SECRET"
    higgsfield_base_url: str = "https://platform.higgsfield.ai"
    higgsfield_video_endpoint: str = "/v1/image2video/dop"
    higgsfield_video_model: str = "dop-turbo"
    # AI video is built from still cartoon scenes, not a text-to-video model:
    # ~0.9 credits a video instead of ~7.5 for five seconds of footage.
    higgsfield_image_endpoint: str = "/v1/text2image/soul"
    higgsfield_image_model: str = "z_image"
    video_image_model_openai: str = "gpt-image-1"
    # Optional still image to animate (text-to-video models can ignore this).
    higgsfield_video_start_image: str = ""

    # Lecture audio/video transcription. "none" = off (free). "openai" = Whisper API.
    transcribe_provider: str = "none"  # "none" | "openai"
    openai_api_key: str = ""
    whisper_model: str = "whisper-1"

    # YouTube link ingestion. YouTube blocks transcript requests coming from
    # datacenter IPs — every request from this container comes back
    # RequestBlocked — so links need someone else's IP to work.
    #
    # Two ways in, tried in this order:
    #   SUPADATA_API_KEY  — a hosted transcript API that owns the unblocked-IP
    #                       problem for us. Preferred: their free tier covers
    #                       100 videos a month and there is nothing to babysit.
    #   YOUTUBE_PROXY_URL — http://user:pass@host:port for a rotating
    #                       residential proxy, if we'd rather run it ourselves.
    # With neither set, YouTube links fail and the user is told to paste text.
    supadata_api_key: str = ""
    youtube_proxy_url: str = ""

    # Natural read-aloud voice. "none" = browser/device voices only (free).
    # "openai" = studio-quality AI voices (reuses OPENAI_API_KEY; costs money).
    tts_provider: str = "none"  # "none" | "openai"
    tts_model: str = "tts-1"
    # Whether AI voice is a paid-plan perk (device voices stay free for all).
    tts_premium_only: bool = True

    # Payments. "dev" = the instant test buttons (no real money). "stripe" =
    # real Stripe Checkout + webhooks (needs a Stripe account + keys).
    billing_provider: str = "dev"  # "dev" | "stripe"
    # Public address of the site. Used for Stripe checkout redirects AND
    # for links inside emails (password reset). Must be the real domain in
    # production or reset links point nowhere.
    app_base_url: str = "http://127.0.0.1:8000"
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_basic: str = ""
    stripe_price_pro: str = ""
    stripe_price_pack_small: str = ""
    stripe_price_pack_medium: str = ""
    stripe_price_pack_large: str = ""
    # Loyalty discount coupons (Stripe coupon IDs) applied automatically at
    # checkout when a user has reached the unlock level. Optional.
    stripe_coupon_10: str = ""  # 10% off — unlocked at level 10
    stripe_coupon_20: str = ""  # 20% off — unlocked at level 20

    # Ads for free users (paid plans never see ads). "none" = placeholder only
    # (no real ads, works offline). "adsense" = real Google AdSense.
    ads_provider: str = "none"  # "none" | "adsense"
    adsense_client_id: str = ""  # "ca-pub-..."
    adsense_slot_home: str = ""
    adsense_slot_quiz: str = ""
    adsense_slot_break: str = ""  # full-screen ad-break unit between study actions

    # Meta (Facebook/Instagram) Pixel — measures which ads drive signups.
    # Empty = no tracking code is served at all.
    meta_pixel_id: str = ""

    # Google Analytics 4. Empty = no analytics code is served at all.
    # Like the Pixel, it is consent-gated and never fires before the visitor
    # accepts cookies.
    ga_measurement_id: str = ""   # "G-XXXXXXXXXX"

    # Server-side traffic dashboard at /admin/stats. Empty = the route does not
    # exist and returns the normal 404, so there is nothing to find or attack.
    # Set it to a long random string to switch the dashboard on.
    admin_key: str = ""


    # ---- Transactional email (password reset) ----
    # "console" prints the email to the server log — fine for development,
    # useless in production. Set to "resend" (or "smtp") before launch or
    # nobody can recover a forgotten password.
    email_provider: str = "console"          # "console" | "resend" | "smtp"
    email_from: str = "StudyForge <noreply@forge.study>"
    resend_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # NOTE: email links reuse APP_BASE_URL (defined above with billing) so
    # there is exactly one source of truth for "where does this site live".
    password_reset_ttl_minutes: int = 60

    # Set to "production" on the live server. Turns the checks below from
    # warnings into hard failures.
    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()


class InsecureConfigError(RuntimeError):
    """Production is misconfigured in a way that would expose users."""


def looks_like_production(s) -> bool:
    """Decide whether this is a real deployment, without trusting a label.

    ENVIRONMENT used to be the only switch, and it defaults to "development".
    So an unset — or merely misspelled ("Production", "prod ") — variable
    silently turned every safety check into a printed warning, and the server
    would happily serve real users while signing JWTs with the dev key that is
    committed to a public repo. Anyone could then mint a token for any account.

    Deployment is now INFERRED from things that are true only in production and
    that nobody sets by accident. A local dev box (SQLite + http + dev billing)
    still boots; a live one cannot be talked out of the checks by a typo.
    """
    if s.environment.strip().lower() in ("production", "prod", "live"):
        return True
    # Deliberately NOT billing_provider: testing Stripe test-mode against a
    # local SQLite database is a legitimate thing to do, and treating it as
    # production would refuse to start for a developer doing exactly that.
    # The real deploy trips all three of the signals below anyway.
    return any((
        s.database_url.startswith(("postgres://", "postgresql://")),
        s.app_base_url.startswith("https://"),
        bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_SERVICE_ID")),
    ))


def verify_production_config(settings: Optional["Settings"] = None) -> List[str]:
    """Refuse to run production with settings that would leak or forge data.

    Returns the list of problems found. In production it raises instead —
    a server that won't start is a bad afternoon; a server running on a
    publicly-known signing key is every account compromised at once.
    """
    s = settings or get_settings()
    problems: List[str] = []

    if s.secret_key == DEV_SECRET_KEY or len(s.secret_key) < 32:
        problems.append(
            "SECRET_KEY is the default or too short. Anyone can forge login "
            "tokens for any account. Set it to 32+ random characters."
        )
    if s.allowed_origins.strip() == "*":
        problems.append(
            "ALLOWED_ORIGINS is '*', so any website can call this API with a "
            "user's credentials. Set it to https://forge.study."
        )
    if s.database_url.startswith("sqlite"):
        problems.append(
            "DATABASE_URL is SQLite. On Railway this is wiped on every deploy "
            "— all user accounts would be lost. Use the Postgres URL."
        )

    if s.email_provider.lower() == "console":
        problems.append(
            "EMAIL_PROVIDER is 'console', so password-reset emails are only "
            "printed to the server log and never delivered. Anyone who forgets "
            "their password is permanently locked out. Set it to 'resend' or 'smtp'."
        )
    if not s.app_base_url.startswith("https://"):
        problems.append(
            "APP_BASE_URL is not https. Password-reset links would be emailed "
            "pointing at an insecure or local address."
        )

    if s.billing_provider.lower() == "stripe":
        for name, value in (
            ("STRIPE_SECRET_KEY", s.stripe_secret_key),
            ("STRIPE_WEBHOOK_SECRET", s.stripe_webhook_secret),
            ("STRIPE_PRICE_BASIC", s.stripe_price_basic),
            ("STRIPE_PRICE_PRO", s.stripe_price_pro),
        ):
            if not value:
                problems.append(
                    name + " is empty while BILLING_PROVIDER=stripe. Customers "
                    "can be charged and never upgraded, with nothing in the logs."
                )

    if problems and looks_like_production(s):
        raise InsecureConfigError(
            "Refusing to start — insecure production configuration:\n  - "
            + "\n  - ".join(problems)
        )
    return problems
