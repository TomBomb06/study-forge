from functools import lru_cache

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
    video_provider: str = "stub"  # "stub" | "higgsfield"

    # Higgsfield Cloud API (get a key at cloud.higgsfield.ai). Credentials are
    # the "KEY_ID:KEY_SECRET" pair. Endpoint/model come from your dashboard —
    # different video models live at different endpoints.
    higgsfield_credentials: str = ""  # "KEY_ID:KEY_SECRET"
    higgsfield_base_url: str = "https://platform.higgsfield.ai"
    higgsfield_video_endpoint: str = "/v1/image2video/dop"
    higgsfield_video_model: str = "dop-turbo"
    # Optional still image to animate (text-to-video models can ignore this).
    higgsfield_video_start_image: str = ""

    # Lecture audio/video transcription. "none" = off (free). "openai" = Whisper API.
    transcribe_provider: str = "none"  # "none" | "openai"
    openai_api_key: str = ""
    whisper_model: str = "whisper-1"

    # Natural read-aloud voice. "none" = browser/device voices only (free).
    # "openai" = studio-quality AI voices (reuses OPENAI_API_KEY; costs money).
    tts_provider: str = "none"  # "none" | "openai"
    tts_model: str = "tts-1"
    # Whether AI voice is a paid-plan perk (device voices stay free for all).
    tts_premium_only: bool = True

    # Payments. "dev" = the instant test buttons (no real money). "stripe" =
    # real Stripe Checkout + webhooks (needs a Stripe account + keys).
    billing_provider: str = "dev"  # "dev" | "stripe"
    app_base_url: str = "http://127.0.0.1:8000"  # for checkout redirect URLs
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
    # Used to build links in emails. Must match the real site or reset links
    # will point somewhere useless.
    app_base_url: str = "https://forge.study"
    password_reset_ttl_minutes: int = 60

    # Set to "production" on the live server. Turns the checks below from
    # warnings into hard failures.
    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()


class InsecureConfigError(RuntimeError):
    """Production is misconfigured in a way that would expose users."""


def verify_production_config(settings: Settings | None = None) -> list[str]:
    """Refuse to run production with settings that would leak or forge data.

    Returns the list of problems found. In production it raises instead —
    a server that won't start is a bad afternoon; a server running on a
    publicly-known signing key is every account compromised at once.
    """
    s = settings or get_settings()
    problems: list[str] = []

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

    if problems and s.environment.lower() == "production":
        raise InsecureConfigError(
            "Refusing to start — insecure production configuration:\n  - "
            + "\n  - ".join(problems)
        )
    return problems
