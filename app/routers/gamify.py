"""Gamification endpoints: game state, events, leaderboard, display name."""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import gamify
from ..auth import get_current_user
from ..db import get_db
from ..models import User

router = APIRouter(tags=["gamify"])


class GameEvent(BaseModel):
    type: str = Field(min_length=1, max_length=30)
    data: Optional[dict] = None
    tz_offset: int = 0


class NameChange(BaseModel):
    name: str = Field(min_length=3, max_length=24)


class RewardBuy(BaseModel):
    item: str = Field(min_length=1, max_length=20)
    tz_offset: int = 0


class ThemeChange(BaseModel):
    theme: str = Field(min_length=1, max_length=20)
    tz_offset: int = 0


@router.get("/me/game")
def get_game(
    tz_offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    state = gamify.ensure_state(user, tz_offset)
    user.game = dict(state)
    db.commit()
    return {
        "display_name": user.display_name,
        "game": gamify.public_state(state),
        "badge_catalog": gamify.BADGES,
    }


@router.post("/gamify/event")
def game_event(
    body: GameEvent,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = gamify.apply_event(user, body.type, body.data, body.tz_offset)
    db.commit()
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result.get("error", "bad event"))
    return result


@router.get("/leaderboard")
def leaderboard(
    tz_offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Top players by weekly XP. Only anonymous display names are exposed."""
    me = gamify.ensure_state(user, tz_offset)
    user.game = dict(me)
    db.commit()

    wk = me["week"]["id"]
    rows = db.scalars(select(User).where(User.game.isnot(None))).all()
    entries = []
    for u in rows:
        g = u.game if isinstance(u.game, dict) else None
        if not g:
            continue
        week = g.get("week") or {}
        if week.get("id") != wk:
            continue  # stale week — counts as 0, skip
        entries.append({
            "name": u.display_name or "Player",
            "level": g.get("level", 1),
            "weekly_xp": int(week.get("xp", 0)),
            "me": u.id == user.id,
        })
    entries.sort(key=lambda e: -e["weekly_xp"])
    my_rank = next((i + 1 for i, e in enumerate(entries) if e["me"]), None)
    return {
        "week": wk,
        "top": entries[:20],
        "my_rank": my_rank,
        "my_weekly_xp": me["week"]["xp"],
        "total_players": len(entries),
    }


@router.post("/rewards/buy")
def buy_reward(
    body: RewardBuy,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = gamify.buy_reward(user, body.item, body.tz_offset)
    db.commit()
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result.get("error", "Can't buy that."))
    return result


@router.post("/me/theme")
def set_theme(
    body: ThemeChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = gamify.set_theme(user, body.theme, body.tz_offset)
    db.commit()
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result.get("error", "Can't set that theme."))
    return result


@router.post("/me/display-name")
def set_display_name(
    body: NameChange,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    name = body.name.strip()
    if not re.fullmatch(r"[A-Za-z0-9 _\-]{3,24}", name):
        raise HTTPException(
            status_code=422,
            detail="Names are 3-24 characters: letters, numbers, spaces, - or _.",
        )
    if "@" in name or "." in name:
        raise HTTPException(status_code=422, detail="Please don't use an email as your name.")
    user.display_name = name
    db.commit()
    return {"display_name": user.display_name}
