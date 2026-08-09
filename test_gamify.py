"""Gamification tests: XP math, levels, streaks, quests, badges, leaderboard."""

import types

from app import gamify


def _user():
    return types.SimpleNamespace(id="u1", display_name=None, game=None)


# ---------- unit: levels ----------

def test_level_curve():
    assert gamify.level_for_xp(0) == 1
    assert gamify.level_for_xp(149) == 1
    assert gamify.level_for_xp(150) == 2
    assert gamify.level_for_xp(450) == 3
    assert gamify.xp_needed_for(2) == 150
    assert gamify.xp_needed_for(3) == 450


def test_display_name_generated_and_stable():
    a = gamify.generate_display_name("abc")
    b = gamify.generate_display_name("abc")
    c = gamify.generate_display_name("other")
    assert a == b
    assert a != c
    assert 5 <= len(a) <= 24


# ---------- unit: events ----------

def test_quiz_event_awards_xp_and_counts():
    u = _user()
    r = gamify.apply_event(u, "quiz", {"score": 4, "total": 5})
    assert r["ok"]
    assert r["gained_xp"] >= 6 * 4  # 6 XP per correct answer (+ maybe quest bonus)
    assert u.game["counters"]["quizzes"] == 1
    assert u.game["counters"]["correct"] == 4
    assert u.display_name  # auto-generated


def test_perfect_quiz_bonus_and_badge():
    u = _user()
    r = gamify.apply_event(u, "quiz", {"score": 5, "total": 5})
    ids = [b["id"] for b in r["new_badges"]]
    assert "perfect_quiz" in ids
    assert "first_quiz" in ids
    assert u.game["counters"]["perfect"] == 1


def test_wrong_answers_earn_no_xp():
    # Rewards come from correct answers only — playing badly earns nothing.
    assert gamify._base_xp("quiz", {"score": 0, "total": 5}) == 0
    assert gamify._base_xp("time_attack", {"score": 0, "total": 10}) == 0
    assert gamify._base_xp("quiz", {"score": 3, "total": 5}) == 18      # 3 correct * 6
    assert gamify._base_xp("quiz", {"score": 5, "total": 5}) == 50      # 30 + perfect 20


def test_all_wrong_quiz_gives_no_coins(monkeypatch):
    """0 correct => 0 XP => 0 coins.

    Daily quests are picked deterministically from the calendar date, and on
    days when "Complete 2 quizzes" is in the rotation the second quiz completes
    that quest and pays out — which made this test fail on roughly 9 days in 14
    depending only on when it ran. Pinning the quest list to empty isolates the
    rule actually under test instead of leaving it to the calendar.
    """
    monkeypatch.setattr(gamify, "quests_for_day", lambda day: [])
    u = _user()
    gamify.apply_event(u, "quiz", {"score": 5, "total": 5})
    coins_before = u.game["coins"]
    r = gamify.apply_event(u, "quiz", {"score": 0, "total": 5})
    assert r["gained_coins"] == 0
    assert u.game["coins"] == coins_before


def test_unknown_event_rejected():
    u = _user()
    r = gamify.apply_event(u, "hack_the_gibson", {})
    assert r["ok"] is False


def test_score_clamped_to_total():
    u = _user()
    r = gamify.apply_event(u, "quiz", {"score": 999, "total": 5})
    assert u.game["counters"]["correct"] == 5  # can't claim more than total


def test_streak_extends_when_daily_goal_met():
    u = _user()
    # A perfect 5-question quiz = 10+20+20 = 50 XP -> meets the daily goal.
    r = gamify.apply_event(u, "quiz", {"score": 5, "total": 5})
    assert u.game["streak"] == 1
    assert r["streak_extended"] is True
    # Second goal-meeting event same day must NOT extend again.
    r2 = gamify.apply_event(u, "quiz", {"score": 5, "total": 5})
    assert u.game["streak"] == 1
    assert r2["streak_extended"] is False


def test_quests_rotate_and_progress():
    day_a = gamify.quests_for_day("2026-07-11")
    day_a2 = gamify.quests_for_day("2026-07-11")
    day_b = gamify.quests_for_day("2026-07-12")
    assert [q["id"] for q in day_a] == [q["id"] for q in day_a2]  # deterministic
    assert len(day_a) == 3
    # Progress: complete lots of quizzes; any quiz-related quest should advance.
    u = _user()
    for _ in range(3):
        gamify.apply_event(u, "quiz", {"score": 5, "total": 5})
    items = u.game["quests"]["items"]
    assert any(q["progress"] > 0 or q["done"] for q in items)


def test_level_up_reported():
    u = _user()
    lvl_ups = []
    for _ in range(6):  # 6 perfect quizzes ≈ 300+ XP -> level 2 at 150
        r = gamify.apply_event(u, "quiz", {"score": 5, "total": 5})
        if r["leveled_up_to"]:
            lvl_ups.append(r["leveled_up_to"])
    assert u.game["level"] >= 2
    assert lvl_ups  # at least one level-up event fired


# ---------- API ----------

def test_game_endpoints_flow(client, auth_headers):
    g = client.get("/me/game", headers=auth_headers)
    assert g.status_code == 200
    body = g.json()
    assert body["display_name"]
    assert body["game"]["level"] == 1
    assert len(body["game"]["quests"]) == 3

    r = client.post("/gamify/event", headers=auth_headers,
                    json={"type": "quiz", "data": {"score": 3, "total": 4}})
    assert r.status_code == 200
    assert r.json()["gained_xp"] >= 6 * 3  # 3 correct * 6 XP

    lb = client.get("/leaderboard", headers=auth_headers)
    assert lb.status_code == 200
    top = lb.json()["top"]
    assert lb.json()["my_rank"] is not None
    me = [e for e in top if e.get("me")]
    assert me and "@" not in me[0]["name"]  # never leaks the email


def test_display_name_change_and_validation(client, auth_headers):
    ok = client.post("/me/display-name", headers=auth_headers,
                     json={"name": "Quiz Wizard 99"})
    assert ok.status_code == 200
    assert ok.json()["display_name"] == "Quiz Wizard 99"
    bad = client.post("/me/display-name", headers=auth_headers,
                      json={"name": "me@example.com"})
    assert bad.status_code == 422


def test_event_endpoint_rejects_garbage(client, auth_headers):
    r = client.post("/gamify/event", headers=auth_headers,
                    json={"type": "steal_xp", "data": {}})
    assert r.status_code == 422


# ---------- rewards ----------

def test_events_earn_coins():
    u = _user()
    r = gamify.apply_event(u, "quiz", {"score": 5, "total": 5})
    assert r["gained_coins"] > 0
    assert u.game["coins"] == r["gained_coins"]


def test_buy_freeze_and_adfree():
    u = _user()
    u.game = gamify.fresh_state()
    u.game["coins"] = 500
    r = gamify.buy_reward(u, "freeze")
    assert r["ok"] and u.game["freezes"] == 1 and u.game["coins"] == 400
    r2 = gamify.buy_reward(u, "adfree")
    assert r2["ok"] and gamify.adfree_active(u) and u.game["coins"] == 340


def test_adfree_is_one_time_only():
    u = _user()
    u.game = gamify.fresh_state()
    u.game["coins"] = 500
    r1 = gamify.buy_reward(u, "adfree")
    assert r1["ok"] and u.game["adfree_claimed"] is True
    coins_after = u.game["coins"]
    # Second purchase must be blocked — and must NOT deduct more coins.
    r2 = gamify.buy_reward(u, "adfree")
    assert r2["ok"] is False
    assert u.game["coins"] == coins_after
    # Freezes stay repeatable (they're a game mechanic, not premium).
    assert gamify.buy_reward(u, "freeze")["ok"] is True


def test_cant_buy_without_coins():
    u = _user()
    u.game = gamify.fresh_state()
    u.game["coins"] = 5
    r = gamify.buy_reward(u, "freeze")
    assert r["ok"] is False


def test_streak_freeze_saves_a_missed_day():
    u = _user()
    u.game = gamify.fresh_state()
    u.game["freezes"] = 1
    u.game["streak"] = 4
    # Last time the goal was met was two days ago (yesterday was missed).
    two_days_ago = gamify.day_key(gamify.local_now(0) - __import__("datetime").timedelta(days=2))
    u.game["last_goal_day"] = two_days_ago
    r = gamify.apply_event(u, "quiz", {"score": 5, "total": 5})  # 50 XP meets goal
    assert r["streak_saved"] is True
    assert u.game["streak"] == 5          # preserved, not reset to 1
    assert u.game["freezes"] == 0         # freeze consumed


def test_theme_locked_until_level():
    u = _user()
    u.game = gamify.fresh_state()  # level 1
    r = gamify.set_theme(u, "sunset")   # needs level 5
    assert r["ok"] is False
    r2 = gamify.set_theme(u, "aurora")  # level 1, always unlocked
    assert r2["ok"] and u.game["theme"] == "aurora"


def test_discount_tiers():
    assert gamify.discount_for(9) == 0
    assert gamify.discount_for(10) == 10
    assert gamify.discount_for(25) == 20


def test_welcome_wheel_is_one_time_and_applies_prize(monkeypatch):
    u = _user()
    u.game = gamify.fresh_state()
    assert u.game["spun"] is False
    # Force the wheel onto each prize type and confirm it actually lands.
    for pid, checks in {
        "xp_150": lambda st: st["xp"] == 150 and st["level"] == gamify.level_for_xp(150),
        "coins_100": lambda st: st["coins"] == 100,
        "freeze_1": lambda st: st["freezes"] == 1,
        "disc_10": lambda st: st["spin_discount"] == 10,
    }.items():
        u.game = gamify.fresh_state()
        idx = next(i for i, p in enumerate(gamify.SPIN_PRIZES) if p["id"] == pid)
        monkeypatch.setattr(gamify.random, "choices", lambda *a, **k: [idx])
        r = gamify.spin_wheel(u)
        assert r["ok"] and r["prize"]["id"] == pid
        assert checks(u.game), pid
        assert u.game["spun"] is True
        # Second spin is always refused, whatever the prize was.
        r2 = gamify.spin_wheel(u)
        assert r2["ok"] is False


def test_wheel_discount_shows_in_public_state_and_survives_low_level():
    u = _user()
    u.game = gamify.fresh_state()
    u.game["spin_discount"] = 20
    pub = gamify.public_state(u.game)
    assert pub["discount_percent"] == 20   # level 1, but wheel gave 20%
    assert pub["spin_discount"] == 20


def test_rewards_api_flow(client, auth_headers):
    g = client.get("/me/game", headers=auth_headers).json()["game"]
    assert "coins" in g and "themes" in g and g["discount_percent"] == 0
    # Earn coins, then it should reflect on the server.
    client.post("/gamify/event", headers=auth_headers,
                json={"type": "quiz", "data": {"score": 5, "total": 5}})
    g2 = client.get("/me/game", headers=auth_headers).json()["game"]
    assert g2["coins"] > 0
    # Buying something you can't afford is a clean 422, not a crash.
    r = client.post("/rewards/buy", headers=auth_headers, json={"item": "freeze"})
    assert r.status_code in (200, 422)


def test_spin_endpoint_once(client, auth_headers):
    g = client.get("/me/game", headers=auth_headers).json()["game"]
    assert g["spun"] is False and len(g["spin_prizes"]) == len(gamify.SPIN_PRIZES)
    r = client.post("/gamify/spin", headers=auth_headers, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and "prize" in body and 0 <= body["index"] < len(gamify.SPIN_PRIZES)
    assert body["state"]["spun"] is True
    # Second call is rejected.
    r2 = client.post("/gamify/spin", headers=auth_headers, json={})
    assert r2.status_code == 422
