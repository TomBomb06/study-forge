from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./studyforge.db"
    secret_key: str = "dev-only-secret-change-me"
    # Comma-separated list of allowed browser origins, or "*" for any.
    allowed_origins: str = "*"
    access_token_expire_minutes: int = 10080  # 7 days
    storage_dir: str = "./storage"
    max_upload_mb: int = 25
    max_media_mb: int = 200  # audio/video lectures are bigger

    generator: str = "mock"  # "mock" | "claude"
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5"

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

    # Ads for free users (paid plans never see ads). "none" = placeholder only
    # (no real ads, works offline). "adsense" = real Google AdSense.
    ads_provider: str = "none"  # "none" | "adsense"
    adsense_client_id: str = ""  # "ca-pub-..."
    adsense_slot_home: str = ""
    adsense_slot_quiz: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
