"""Read-aloud voice: list options, save the user's pick, and stream
studio-quality AI audio (OpenAI TTS) for the notes.

Device (browser) voices are always available and free. AI voices require
TTS_PROVIDER=openai + an OPENAI_API_KEY, and — when tts_premium_only is on —
a paid plan. The client falls back to device voices whenever AI isn't
available to that user.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import billing
from ..auth import get_current_user
from ..db import get_db
from ..models import User
from ..pipeline import tts

router = APIRouter(tags=["voice"])


class VoiceSave(BaseModel):
    voice: str = Field(min_length=1, max_length=64)


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    voice: Optional[str] = None
    speed: float = 1.0


@router.get("/voice/options")
def voice_options(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    vs = billing.voice_status(user)
    db.commit()  # persist any monthly reset
    return {
        "ai_enabled": tts.is_enabled(),
        # AI voices are offered to everyone (free tier included) while the
        # server has a key; the monthly character quota governs actual use.
        "ai_available": tts.is_enabled(),
        "voices": tts.VOICES,
        "default": tts.DEFAULT_VOICE,
        "selected": user.voice or "",
        "plan": vs["plan"],
        "monthly_chars": vs["monthly_chars"],
        "chars_used": vs["chars_used"],
        "chars_remaining": vs["remaining"],
    }


@router.post("/me/voice")
def save_voice(
    body: VoiceSave,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user.voice = body.voice.strip()[:64]
    db.commit()
    return {"voice": user.voice}


@router.post("/tts/speak")
def speak(
    body: SpeakRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not tts.is_enabled():
        raise HTTPException(status_code=503, detail="AI voice isn't enabled on this server.")
    # Free-with-a-cap: everyone can use AI voice until their monthly character
    # allowance runs out, then it's a 402 that sends them to upgrade.
    vs = billing.voice_status(user)
    db.commit()
    if not vs["can_use"]:
        raise HTTPException(
            status_code=402,
            detail="You've used up this month's free AI voice. Upgrade for much "
                   "more natural read-aloud time.",
        )
    voice = tts.valid_voice(body.voice or user.voice or tts.DEFAULT_VOICE)
    try:
        audio = tts.synthesize(body.text, voice=voice, speed=body.speed)
    except tts.TTSError as e:
        raise HTTPException(status_code=502, detail=str(e))
    billing.consume_tts_chars(user, len(body.text or ""))
    db.commit()
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Cache-Control": "no-store"})
