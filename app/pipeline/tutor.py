"""Premium 'Teach me' tutor + personalized review.

Turns a quiz miss, a whole batch of missed questions, a flashcard, or a
set's notes into an in-depth, step-by-step explanation that actually
teaches — the Photomath-Plus-style value that upgraded plans unlock.

Gated to paid plans at the router level. When no AI key is configured
(GENERATOR != "claude"), we return a genuinely useful *templated*
explanation so local dev and the test suite work without spending tokens.
"""

from typing import Optional

from ..config import get_settings

MODES = ("review", "question", "card", "notes")
MAX_ITEMS = 12
_SUMMARY_CTX_CHARS = 6000


class TutorError(Exception):
    """User-facing tutor problem (AI/network)."""


_SYSTEM = (
    "You are an encouraging, expert one-on-one tutor. You build genuine "
    "understanding step by step, in plain language a motivated student can "
    "follow. You use the student's own study material as the source of truth "
    "and never invent facts. You are warm and motivating, never condescending. "
    "You always answer in clean Markdown."
)


def _clip(v, n: int) -> str:
    return str(v if v is not None else "")[:n]


def _fmt_items(items: list) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        it = it if isinstance(it, dict) else {}
        lines.append(f"{i}. Question: {_clip(it.get('question'), 600)}")
        choices = it.get("choices")
        if isinstance(choices, list) and choices:
            lines.append("   Options: " + " | ".join(_clip(c, 120) for c in choices[:6]))
        if _clip(it.get("correct"), 1):
            lines.append(f"   Correct answer: {_clip(it.get('correct'), 300)}")
        if _clip(it.get("your_answer"), 1):
            lines.append(f"   Student answered: {_clip(it.get('your_answer'), 300)}")
    return "\n".join(lines)


def _context(summary: str) -> str:
    summary = (summary or "").strip()
    if not summary:
        return ""
    return (
        "\n\nThe student's study notes, for context (treat as the source of "
        f"truth):\n\"\"\"\n{summary[:_SUMMARY_CTX_CHARS]}\n\"\"\""
    )


def _build_prompt(mode: str, payload: dict, summary: str) -> str:
    items = [i for i in (payload.get("items") or []) if isinstance(i, dict)][:MAX_ITEMS]
    ctx = _context(summary)
    if mode == "review":
        return (
            "The student just finished a quiz and got the questions below wrong. "
            "Write a warm, personalized review that helps them actually learn what "
            "they missed.\n\nFor EACH question:\n"
            "- Restate the idea being tested in one line.\n"
            "- Explain the correct answer and WHY it is right, step by step.\n"
            "- If the student's answer is given, explain why it was a tempting mistake.\n"
            "- Give a quick memory hook or tip so it sticks.\n\n"
            "Then finish with a short, encouraging 'What to focus on next' with 2-3 "
            f"concrete suggestions.\n\nMISSED QUESTIONS:\n{_fmt_items(items)}{ctx}"
        )
    if mode == "question":
        one = items[0] if items else {}
        return (
            "Teach the student this one question in depth so they truly understand "
            "it — not just the answer. Explain the underlying concept, walk through "
            "the reasoning to the correct answer step by step, say why each wrong "
            "option is wrong, and end with a memory hook.\n\n"
            f"{_fmt_items([one])}{ctx}"
        )
    if mode == "card":
        one = items[0] if items else {}
        return (
            "Expand this flashcard into a richer teaching moment. Explain the concept "
            "behind it in depth, give a concrete example or analogy, and add one common "
            "misconception to avoid.\n\n"
            f"Flashcard front: {_clip(one.get('front'), 600)}\n"
            f"Flashcard back: {_clip(one.get('back'), 1200)}{ctx}"
        )
    # notes / go deeper
    return (
        "Take the student's study notes and go deeper: expand the most important "
        "concepts with clearer explanations, worked examples or analogies, and the "
        "connections between ideas — so a student who found the notes too brief can "
        "really understand the material. Organize it with clear Markdown headings."
        f"{ctx}"
    )


# ---------------------------------------------------------------- mock

def _mock(mode: str, payload: dict, summary: str) -> str:
    items = [i for i in (payload.get("items") or []) if isinstance(i, dict)]
    if mode == "review":
        parts = ["## Your personalized review\n",
                 "Here's a closer look at what tripped you up — you've got this.\n"]
        for i, it in enumerate(items[:MAX_ITEMS], 1):
            q = _clip(it.get("question"), 400)
            correct = _clip(it.get("correct"), 300)
            yours = _clip(it.get("your_answer"), 300)
            parts.append(f"### {i}. {q}")
            if correct:
                parts.append(f"**Correct answer:** {correct}")
            if yours:
                parts.append(f"You answered *{yours}* — a common mix-up. Re-read the "
                             "note below and the difference should click.")
            parts.append("**Why:** This comes straight from your notes — focus on the "
                         "key term and what makes it different from the alternatives.\n"
                         "**Remember it:** tie it to one concrete example you already know.\n")
        parts.append("### What to focus on next\nRe-do this quiz once more, review the "
                     "flashcards for these terms, and you'll lock them in. 💪")
        return "\n".join(parts)
    if mode == "question":
        one = items[0] if items else {}
        return (f"## Let's break this down\n\n**Question:** {_clip(one.get('question'), 400)}\n\n"
                f"**Correct answer:** {_clip(one.get('correct'), 300)}\n\n"
                "**Step by step:** Start from the core concept in your notes, then eliminate "
                "each option that doesn't fit. The right choice is the one your material "
                "directly supports.\n\n**Memory hook:** link it to one vivid example so it sticks.")
    if mode == "card":
        one = items[0] if items else {}
        return (f"## Going deeper\n\n**{_clip(one.get('front'), 300)}**\n\n"
                f"{_clip(one.get('back'), 600)}\n\n**In your own words:** think of a real example "
                "of this idea. **Watch out for:** the most common misconception is confusing it "
                "with a closely related term — keep the distinction clear.")
    return ("## Going deeper on your notes\n\nHere the key concepts are expanded with examples "
            "and how they connect. Re-read each section and try to explain it out loud in your "
            "own words — that's when it truly sticks.")


# ---------------------------------------------------------------- entry

def explain(mode: str, payload: Optional[dict] = None, summary: str = "") -> str:
    """Generate an in-depth explanation. Returns Markdown."""
    payload = payload if isinstance(payload, dict) else {}
    if mode not in MODES:
        mode = "question"
    settings = get_settings()
    if settings.generator == "claude" and settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            resp = client.messages.create(
                model=settings.claude_model,
                max_tokens=2200,
                system=_SYSTEM,
                messages=[{"role": "user", "content": _build_prompt(mode, payload, summary)}],
            )
            txt = "".join(getattr(b, "text", "") or "" for b in (resp.content or [])).strip()
            if txt:
                return txt
        except Exception as e:  # network/auth/rate-limit
            raise TutorError(f"The tutor couldn't respond right now: {e}") from e
    return _mock(mode, payload, summary)
