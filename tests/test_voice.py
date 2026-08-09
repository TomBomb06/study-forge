"""Read-aloud voice: options, saving a preference, and the free-with-a-cap
AI-voice quota (free users get an allowance, then a 402 upsell)."""

import uuid


def _signup(client):
    email = f"voice-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return email, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _set_tts_used(email, used):
    from app.db import SessionLocal
    from app.models import User
    from app import billing
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).one()
        billing.ensure_tts_period(u)   # stamp current period
        u.tts_chars_used = used
        db.commit()
    finally:
        db.close()


def test_voice_options_shape(client):
    _, headers = _signup(client)
    r = client.get("/voice/options", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ai_enabled"] is False          # TTS off by default in tests
    assert isinstance(body["voices"], list) and len(body["voices"]) >= 4
    assert body["default"] == "nova"
    assert body["selected"] == ""
    # Free monthly quota is exposed so the UI can show what's left.
    assert body["monthly_chars"] > 0
    assert body["chars_remaining"] == body["monthly_chars"]
    assert body["plan"] == "free"


def test_save_and_read_back_voice(client):
    _, headers = _signup(client)
    ok = client.post("/me/voice", headers=headers, json={"voice": "nova"})
    assert ok.status_code == 200 and ok.json()["voice"] == "nova"
    assert client.get("/voice/options", headers=headers).json()["selected"] == "nova"


def test_tts_unavailable_when_disabled(client):
    _, headers = _signup(client)
    r = client.post("/tts/speak", headers=headers, json={"text": "Hello", "voice": "nova"})
    assert r.status_code == 503  # provider off


def test_free_user_can_use_then_hits_402(client, monkeypatch):
    from app import config
    from app.pipeline import tts
    settings = config.get_settings()
    monkeypatch.setattr(settings, "tts_provider", "openai", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test", raising=False)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice="nova", speed=1.0: b"ID3AUDIO")

    email, headers = _signup(client)
    # Within the free allowance → returns audio.
    r = client.post("/tts/speak", headers=headers, json={"text": "hello there", "voice": "nova"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.content == b"ID3AUDIO"

    # Now exhaust the monthly allowance → 402 upsell.
    _set_tts_used(email, 10_000_000)
    r2 = client.post("/tts/speak", headers=headers, json={"text": "more", "voice": "nova"})
    assert r2.status_code == 402
    assert "upgrade" in r2.json()["detail"].lower()


def test_usage_is_metered(client, monkeypatch):
    from app import config
    from app.pipeline import tts
    settings = config.get_settings()
    monkeypatch.setattr(settings, "tts_provider", "openai", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test", raising=False)
    monkeypatch.setattr(tts, "synthesize", lambda text, voice="nova", speed=1.0: b"A")

    _, headers = _signup(client)
    before = client.get("/voice/options", headers=headers).json()["chars_remaining"]
    client.post("/tts/speak", headers=headers, json={"text": "12345", "voice": "nova"})
    after = client.get("/voice/options", headers=headers).json()["chars_remaining"]
    assert before - after == 5  # 5 characters consumed


def test_valid_voice_falls_back_to_default():
    from app.pipeline import tts
    assert tts.valid_voice("nova") == "nova"
    assert tts.valid_voice("not-a-voice") == tts.DEFAULT_VOICE
