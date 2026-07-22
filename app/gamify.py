"""Gamification engine: XP, levels, streaks, daily goal, quests, badges,
and weekly leaderboard scores.

All rules are server-side so scores can't be forged from the client. The
whole game state lives in one JSON blob on the user row (User.game), which
keeps migrations trivial. The client sends events ("I finished a quiz,
7/8"); the server decides what they're worth.

Day boundaries use the CLIENT's local day (client sends its UTC offset in
minutes) so streaks roll over at the student's midnight, not UTC's.
"""

import copy
import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

DAILY_GOAL_XP = 50
QUEST_REWARD_XP = 30
BADGE_REWARD_XP = 25


# ---------- time helpers ----------

def local_now(tz_offset_min: int) -> datetime:
    """Client-local time from its UTC offset (JS getTimezoneOffset sign)."""
    try:
        off = max(-14 * 60, min(14 * 60, int(tz_offset_min or 0)))
    except (TypeError, ValueError):
        off = 0
    # JS getTimezoneOffset() is minutes to ADD to local to get UTC,
    # so local = utc - offset.
    return datetime.now(timezone.utc) - timedelta(minutes=off)


def day_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ---------- levels ----------

def xp_needed_for(level: int) -> int:
    """Total XP required to REACH a level (level 1 = 0 XP)."""
    if level <= 1:
        return 0
    n = level - 1
    return 75 * n * (n + 1)  # L2=150, L3=450, L4=900, L5=1500...


def level_for_xp(xp: int) -> int:
    level = 1
    while xp >= xp_needed_for(level + 1):
        level += 1
        if level >= 200:
            break
    return level


# ---------- display names ----------

_ADJ = ["Swift", "Clever", "Brave", "Mighty", "Golden", "Cosmic", "Turbo",
        "Lucky", "Silent", "Blazing", "Frost", "Shadow", "Neon", "Atomic",
        "Crimson", "Electric", "Epic", "Nimble", "Solar", "Thunder"]
_NOUN = ["Falcon", "Tiger", "Wizard", "Comet", "Panda", "Dragon", "Otter",
         "Phoenix", "Wolf", "Raven", "Koala", "Viper", "Knight", "Fox",
         "Orca", "Lynx", "Griffin", "Badger", "Hawk", "Yeti"]


def generate_display_name(seed: str) -> str:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    rng = random.Random(h)
    return f"{rng.choice(_ADJ)}{rng.choice(_NOUN)}{rng.randint(10, 99)}"


# ---------- badges ----------

BADGES = [
    {"id": "first_set", "name": "Forger", "icon": "🛠️", "desc": "Create your first study set"},
    {"id": "sets_5", "name": "Collector", "icon": "📚", "desc": "Create 5 study sets"},
    {"id": "sets_15", "name": "Librarian", "icon": "🏛️", "desc": "Create 15 study sets"},
    {"id": "first_quiz", "name": "Challenger", "icon": "🎯", "desc": "Finish your first quiz"},
    {"id": "perfect_quiz", "name": "Flawless", "icon": "💎", "desc": "Score 100% on a quiz"},
    {"id": "perfect_5", "name": "Untouchable", "icon": "👑", "desc": "Score 100% five times"},
    {"id": "streak_3", "name": "Warming Up", "icon": "🔥", "desc": "3-day streak"},
    {"id": "streak_7", "name": "On Fire", "icon": "🚒", "desc": "7-day streak"},
    {"id": "streak_30", "name": "Unstoppable", "icon": "🌋", "desc": "30-day streak"},
    {"id": "level_5", "name": "Rising Star", "icon": "⭐", "desc": "Reach level 5"},
    {"id": "level_10", "name": "Scholar", "icon": "🎓", "desc": "Reach level 10"},
    {"id": "correct_100", "name": "Century", "icon": "💯", "desc": "100 correct answers"},
    {"id": "correct_500", "name": "Brainiac", "icon": "🧠", "desc": "500 correct answers"},
    {"id": "cards_100", "name": "Card Shark", "icon": "🃏", "desc": "Flip 100 flashcards"},
    {"id": "match_10", "name": "Matchmaker", "icon": "🧩", "desc": "Finish 10 matching games"},
    {"id": "night_owl", "name": "Night Owl", "icon": "🦉", "desc": "Study after 10pm"},
    {"id": "early_bird", "name": "Early Bird", "icon": "🐦", "desc": "Study before 8am"},
    {"id": "sharer", "name": "Team Player", "icon": "🤝", "desc": "Share a study set"},
]
_BADGE_IDS = {b["id"] for b in BADGES}


# ---------- quests ----------

QUEST_POOL = [
    {"id": "q_quiz2", "name": "Complete 2 quizzes", "target": 2},
    {"id": "q_score80", "name": "Score 80%+ on a quiz", "target": 1},
    {"id": "q_sessions3", "name": "Finish 3 study sessions", "target": 3},
    {"id": "q_cards20", "name": "Flip 20 flashcards", "target": 20},
    {"id": "q_match1", "name": "Win a matching game", "target": 1},
    {"id": "q_test1", "name": "Complete a practice test", "target": 1},
    {"id": "q_xp100", "name": "Earn 100 XP today", "target": 100},
]


def quests_for_day(day: str) -> list:
    """3 deterministic daily quests — same for everyone, rotates daily."""
    h = int(hashlib.sha256(("quests:" + day).encode()).hexdigest(), 16)
    rng = random.Random(h)
    picks = rng.sample(QUEST_POOL, 3)
    return [
        {"id": q["id"], "name": q["name"], "target": q["target"],
         "progress": 0, "done": False}
        for q in picks
    ]


# ---------- state ----------

def fresh_state() -> dict:
    return {
        "xp": 0, "level": 1,
        "streak": 0, "best_streak": 0,
        "last_goal_day": "", "goal": DAILY_GOAL_XP,
        "daily": {"day": "", "xp": 0},
        "week": {"id": "", "xp": 0},
        "badges": [],
        "quests": {"day": "", "items": []},
        "counters": {"sets": 0, "quizzes": 0, "tests": 0, "perfect": 0,
                     "correct": 0, "matches": 0, "cards": 0, "shares": 0,
                     "sessions": 0, "ta_best": 0},
    }


def _rollover(state: dict, now: datetime) -> None:
    """Reset daily/weekly/quest buckets when the local day or week changed."""
    today = day_key(now)
    if state["daily"].get("day") != today:
        state["daily"] = {"day": today, "xp": 0}
    wk = week_key(now)
    if state["week"].get("id") != wk:
        state["week"] = {"id": wk, "xp": 0}
    if state["quests"].get("day") != today:
        state["quests"] = {"day": today, "items": quests_for_day(today)}


def _flag_game_dirty(user) -> None:
    """Tell SQLAlchemy the JSON column changed. Plain JSON columns compare by
    equality at flush time, so in-place edits of the loaded dict would
    otherwise be silently skipped. No-op for plain test doubles."""
    try:
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(user, "game")
    except Exception:
        pass


def ensure_state(user, tz_offset_min: int = 0) -> dict:
    """Return the user's game state, initializing and rolling over as needed.

    Deep-copies the stored blob so we never mutate the exact object SQLAlchemy
    loaded (see _flag_game_dirty for why that matters).
    """
    if isinstance(user.game, dict) and user.game.get("counters"):
        state = copy.deepcopy(user.game)
    else:
        state = fresh_state()
    now = local_now(tz_offset_min)
    _rollover(state, now)
    if not user.display_name:
        user.display_name = generate_display_name(user.id)
    user.game = state
    _flag_game_dirty(user)
    return state


# ---------- XP rules ----------

def _base_xp(etype: str, data: dict) -> int:
    def _int(key, default=0, lo=0, hi=1000):
        try:
            return max(lo, min(hi, int(data.get(key, default))))
        except (TypeError, ValueError):
            return default

    if etype == "set_created":
        return 25
    if etype in ("quiz", "test", "time_attack"):
        score = _int("score", 0, 0, 200)
        total = _int("total", 1, 1, 200)
        score = min(score, total)
        xp = 10 + 4 * score
        if total >= 3 and score == total:
            xp += 20  # perfect bonus
        return xp
    if etype == "match":
        return 15 + min(_int("pairs", 4, 0, 30), 30)
    if etype == "cards":
        return min(_int("count", 0, 0, 30), 30)
    if etype == "study":  # opened guide / read-aloud / video session
        return 5
    if etype == "share":
        return 10
    return 0


_VALID_EVENTS = {"set_created", "quiz", "test", "time_attack", "match",
                 "cards", "study", "share"}


def _quest_progress(items: list, etype: str, data: dict, daily_xp: int) -> int:
    """Advance quests; returns bonus XP for quests completed just now."""
    bonus = 0
    pct = 0
    if etype in ("quiz", "test", "time_attack"):
        try:
            total = max(1, int(data.get("total", 1)))
            pct = round(100 * min(int(data.get("score", 0)), total) / total)
        except (TypeError, ValueError):
            pct = 0
    session_events = {"quiz", "test", "time_attack", "match", "cards", "study"}
    for q in items:
        if q["done"]:
            continue
        qid = q["id"]
        if qid == "q_quiz2" and etype == "quiz":
            q["progress"] += 1
        elif qid == "q_score80" and etype == "quiz" and pct >= 80:
            q["progress"] += 1
        elif qid == "q_sessions3" and etype in session_events:
            q["progress"] += 1
        elif qid == "q_cards20" and etype == "cards":
            try:
                q["progress"] += max(0, min(30, int(data.get("count", 0))))
            except (TypeError, ValueError):
                pass
        elif qid == "q_match1" and etype == "match":
            q["progress"] += 1
        elif qid == "q_test1" and etype == "test":
            q["progress"] += 1
        elif qid == "q_xp100":
            q["progress"] = daily_xp
        if q["progress"] >= q["target"]:
            q["progress"] = q["target"]
            q["done"] = True
            bonus += QUEST_REWARD_XP
    return bonus


def _check_badges(state: dict, hour: int) -> list:
    """Award any newly-earned badges; returns list of badge dicts."""
    c = state["counters"]
    earned = set(state["badges"])
    new = []

    def award(bid):
        if bid in _BADGE_IDS and bid not in earned:
            earned.add(bid)
            new.append(bid)

    if c["sets"] >= 1:
        award("first_set")
    if c["sets"] >= 5:
        award("sets_5")
    if c["sets"] >= 15:
        award("sets_15")
    if c["quizzes"] >= 1:
        award("first_quiz")
    if c["perfect"] >= 1:
        award("perfect_quiz")
    if c["perfect"] >= 5:
        award("perfect_5")
    if state["streak"] >= 3:
        award("streak_3")
    if state["streak"] >= 7:
        award("streak_7")
    if state["streak"] >= 30:
        award("streak_30")
    if state["level"] >= 5:
        award("level_5")
    if state["level"] >= 10:
        award("level_10")
    if c["correct"] >= 100:
        award("correct_100")
    if c["correct"] >= 500:
        award("correct_500")
    if c["cards"] >= 100:
        award("cards_100")
    if c["matches"] >= 10:
        award("match_10")
    if hour >= 22 or hour < 5:
        award("night_owl")
    if 5 <= hour < 8:
        award("early_bird")
    if c["shares"] >= 1:
        award("sharer")

    state["badges"] = sorted(earned)
    return [b for b in BADGES if b["id"] in new]


def apply_event(user, etype: str, data: Optional[dict], tz_offset_min: int = 0) -> dict:
    """Apply one game event to the user's state; returns a result summary."""
    data = data if isinstance(data, dict) else {}
    if etype not in _VALID_EVENTS:
        state = ensure_state(user, tz_offset_min)
        return {"ok": False, "error": "unknown event", "state": state}

    state = ensure_state(user, tz_offset_min)
    now = local_now(tz_offset_min)
    today = day_key(now)
    before_level = state["level"]

    # --- counters ---
    c = state["counters"]
    try:
        score = max(0, min(200, int(data.get("score", 0))))
        total = max(1, min(200, int(data.get("total", 1))))
    except (TypeError, ValueError):
        score, total = 0, 1
    score = min(score, total)
    if etype == "set_created":
        c["sets"] += 1
    elif etype == "quiz":
        c["quizzes"] += 1
        c["correct"] += score
        if total >= 3 and score == total:
            c["perfect"] += 1
    elif etype == "test":
        c["tests"] += 1
        c["correct"] += score
        if total >= 3 and score == total:
            c["perfect"] += 1
    elif etype == "time_attack":
        c["correct"] += score
        c["ta_best"] = max(c["ta_best"], score)
    elif etype == "match":
        c["matches"] += 1
    elif etype == "cards":
        try:
            c["cards"] += max(0, min(30, int(data.get("count", 0))))
        except (TypeError, ValueError):
            pass
    elif etype == "share":
        c["shares"] += 1
    if etype in ("quiz", "test", "time_attack", "match", "cards", "study"):
        c["sessions"] += 1

    # --- XP ---
    gained = _base_xp(etype, data)
    state["daily"]["xp"] += gained
    quest_bonus = _quest_progress(state["quests"]["items"], etype, data,
                                  state["daily"]["xp"])
    gained += quest_bonus
    state["daily"]["xp"] += quest_bonus

    # --- streak: extends the day the daily goal is met ---
    streak_extended = False
    if state["daily"]["xp"] >= state["goal"] and state["last_goal_day"] != today:
        yesterday = day_key(now - timedelta(days=1))
        state["streak"] = state["streak"] + 1 if state["last_goal_day"] == yesterday else 1
        state["best_streak"] = max(state["best_streak"], state["streak"])
        state["last_goal_day"] = today
        streak_extended = True

    # --- badges (may add XP too) ---
    state["xp"] += gained
    state["week"]["xp"] += gained
    state["level"] = level_for_xp(state["xp"])
    new_badges = _check_badges(state, now.hour)
    if new_badges:
        badge_xp = BADGE_REWARD_XP * len(new_badges)
        gained += badge_xp
        state["xp"] += badge_xp
        state["week"]["xp"] += badge_xp
        state["daily"]["xp"] += badge_xp
        state["level"] = level_for_xp(state["xp"])

    leveled_up_to = state["level"] if state["level"] > before_level else None

    user.game = dict(state)
    _flag_game_dirty(user)
    return {
        "ok": True,
        "gained_xp": gained,
        "leveled_up_to": leveled_up_to,
        "streak_extended": streak_extended,
        "new_badges": new_badges,
        "state": public_state(state),
    }


def public_state(state: dict) -> dict:
    """Shape the state for the client, with level-progress math done."""
    level = state["level"]
    cur = xp_needed_for(level)
    nxt = xp_needed_for(level + 1)
    return {
        "xp": state["xp"],
        "level": level,
        "level_xp": state["xp"] - cur,
        "level_span": nxt - cur,
        "streak": state["streak"],
        "best_streak": state["best_streak"],
        "daily_xp": state["daily"]["xp"],
        "goal": state["goal"],
        "goal_met": state["daily"]["xp"] >= state["goal"],
        "weekly_xp": state["week"]["xp"],
        "quests": state["quests"]["items"],
        "badges": state["badges"],
        "counters": state["counters"],
    }
