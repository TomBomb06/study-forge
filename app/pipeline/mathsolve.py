"""Math photo solver — the Photomath-style hook.

A student snaps a photo of a problem; we read it with vision AI and return
two separately-gated things:

  * `answer`  — always free. This is the hook that makes people come back
                daily and tell their friends about the app.
  * `steps`   — Premium. *How* to solve it: the worked reasoning, line by
                line. Free users see the answer plus a blurred teaser.

Keeping the answer free and the method paid is deliberate: the answer is
what earns the habit, the method is what earns the money — and it's also
the honest split, because a student who wants to actually learn is exactly
the one worth charging.

When no AI key is configured (GENERATOR != "claude") we fall back to a small
local solver for simple arithmetic/linear equations. If that can't handle the
problem we raise — we never invent an answer, because a confident wrong answer
on homework is worse than no answer at all.
"""

import base64
import json
import re
from typing import Optional

from ..config import get_settings

# Vision input limits — a phone photo of one homework problem.
MAX_IMAGE_BYTES = 6 * 1024 * 1024
SUPPORTED_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
MAX_PROBLEM_CHARS = 2000


class MathError(Exception):
    """User-facing solver problem (unreadable photo, AI/network)."""


_SYSTEM = (
    "You are an expert, patient math tutor reading a photo of a student's "
    "homework problem. You are meticulous and never guess: you re-check your "
    "arithmetic before answering. You always reply with valid JSON only — no "
    "prose outside the JSON, no markdown fences."
)

_PROMPT = (
    "Read the math problem in this image and solve it.\n\n"
    "Reply with ONLY a JSON object with exactly these keys:\n"
    '  "problem"  — the problem exactly as written, in plain text/LaTeX-free notation\n'
    '  "answer"   — the final answer, as short as possible (e.g. "x = 7" or "24 cm²")\n'
    '  "steps"    — an array of 2-6 strings; each is ONE clear step of the working, '
    'in plain language a student can follow. Show the algebra, don\'t just assert.\n'
    '  "topic"    — a 1-4 word label for the skill being practiced (e.g. "Quadratic equations")\n'
    '  "check"    — one short sentence showing how to verify the answer is right\n\n'
    "If the image contains no readable math problem, reply exactly: "
    '{"error": "no_problem"}\n'
    "If there are several problems, solve only the first/clearest one."
)


# ------------------------------------------------------------------ helpers

def media_type_for(ext: str) -> Optional[str]:
    return SUPPORTED_MEDIA.get((ext or "").lower())


def _coerce(raw: str) -> dict:
    """Pull a JSON object out of a model reply, tolerating stray fences."""
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
    try:
        data = json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            raise MathError("The solver couldn't read that photo. Try a clearer, "
                            "closer shot of just the problem.")
        try:
            data = json.loads(m.group(0))
        except Exception:
            raise MathError("The solver couldn't read that photo. Try a clearer, "
                            "closer shot of just the problem.")
    if not isinstance(data, dict):
        raise MathError("The solver couldn't read that photo. Try again.")
    return data


def _clean(data: dict) -> dict:
    if data.get("error") == "no_problem":
        raise MathError("I couldn't find a math problem in that photo. Make sure "
                        "the problem fills most of the frame and is in focus.")
    steps = data.get("steps")
    if isinstance(steps, str):
        steps = [s for s in re.split(r"\n+", steps) if s.strip()]
    if not isinstance(steps, list):
        steps = []
    steps = [str(s).strip()[:400] for s in steps if str(s).strip()][:8]
    answer = str(data.get("answer") or "").strip()[:200]
    if not answer:
        raise MathError("The solver couldn't get a confident answer for that one. "
                        "Try a clearer photo, or type the problem out.")
    return {
        "problem": str(data.get("problem") or "").strip()[:MAX_PROBLEM_CHARS],
        "answer": answer,
        "steps": steps,
        "topic": str(data.get("topic") or "").strip()[:60],
        "check": str(data.get("check") or "").strip()[:300],
    }


# ------------------------------------------------------------------ fallback

_SAFE_EXPR = re.compile(r"^[0-9+\-*/^(). \t]+$")


def _local_solve(text: str) -> Optional[dict]:
    """Solve plain arithmetic and simple `ax + b = c` without any AI.

    Used when no API key is set (dev/tests) and as a sanity net.
    """
    t = (text or "").strip().rstrip("=?").strip()
    if not t:
        return None

    # ax + b = c   /   ax - b = c   /   ax = c
    m = re.match(r"^\s*(-?\d*\.?\d*)\s*\*?\s*x\s*([+-]\s*\d+\.?\d*)?\s*=\s*(-?\d+\.?\d*)\s*$",
                 t, re.I)
    if m:
        a_raw, b_raw, c_raw = m.group(1), m.group(2), m.group(3)
        a = float(a_raw) if a_raw not in ("", "-", "+") else (-1.0 if a_raw == "-" else 1.0)
        b = float((b_raw or "0").replace(" ", ""))
        c = float(c_raw)
        if a == 0:
            return None
        x = (c - b) / a
        xs = f"{x:g}"
        steps = []
        if b:
            # Phrase it the way a teacher would: "add 9", not "subtract -9".
            verb = "Subtract" if b > 0 else "Add"
            steps.append(f"{verb} {abs(b):g} from both sides to get the x-term alone: "
                         f"{a:g}x = {c - b:g}." if b > 0 else
                         f"{verb} {abs(b):g} to both sides to get the x-term alone: "
                         f"{a:g}x = {c - b:g}.")
        if a != 1:
            steps.append(f"Divide both sides by {a:g}: x = {c - b:g} ÷ {a:g}.")
        steps.append(f"So x = {xs}.")
        return {"problem": t, "answer": f"x = {xs}", "steps": steps,
                "topic": "Linear equations",
                "check": f"Put x = {xs} back in: it makes both sides equal {c:g}."}

    # plain arithmetic
    expr = t.replace("^", "**").replace("×", "*").replace("÷", "/")
    if _SAFE_EXPR.match(expr.replace("**", "^").replace("^", "")) or _SAFE_EXPR.match(expr):
        try:
            val = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 - regex-gated arithmetic only
        except Exception:
            return None
        if isinstance(val, (int, float)):
            vs = f"{val:g}"
            return {"problem": t, "answer": vs,
                    "steps": ["Work left to right, doing brackets and powers first, "
                              "then multiplication and division, then addition and "
                              "subtraction.",
                              f"That gives {vs}."],
                    "topic": "Order of operations",
                    "check": f"Re-run the calculation in the same order — you should get {vs} again."}
    return None


_UNAVAILABLE = ("The math solver isn't available right now. Please try again in a "
                "little while — or type the problem out and we'll have a go.")


def _offline(text_hint: str = "") -> dict:
    """No AI key configured: solve it locally or admit we can't.

    Deliberately never invents an answer. A confidently wrong answer on a
    student's homework is far worse than "we couldn't do this one" — they'd
    hand it in. This is why there is no canned fallback here.
    """
    local = _local_solve(text_hint)
    if local:
        return local
    raise MathError(_UNAVAILABLE)


# ------------------------------------------------------------------ entry

def solve_image(image_bytes: bytes, media_type: str) -> dict:
    """Solve a math problem from a photo. Returns problem/answer/steps/topic/check."""
    if not image_bytes:
        raise MathError("No photo received. Try taking the picture again.")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise MathError("That photo is too large — try a smaller or lower-resolution shot.")

    settings = get_settings()
    if settings.generator == "claude" and settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            resp = client.messages.create(
                # Always the sharper model: a wrong answer is worse than no answer,
                # and this is the feature people judge the whole app by.
                model=settings.claude_model,
                max_tokens=1200,
                system=_SYSTEM,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type,
                        "data": base64.b64encode(image_bytes).decode("ascii")}},
                    {"type": "text", "text": _PROMPT},
                ]}],
            )
            raw = "".join(getattr(b, "text", "") or "" for b in (resp.content or []))
            return _clean(_coerce(raw))
        except MathError:
            raise
        except Exception as e:  # network/auth/rate-limit
            raise MathError(f"The solver couldn't respond right now: {e}") from e
    # No AI key: we cannot read an image at all, so say so plainly.
    raise MathError(_UNAVAILABLE)


def solve_text(problem: str) -> dict:
    """Solve a typed-out math problem (no photo)."""
    problem = (problem or "").strip()
    if not problem:
        raise MathError("Type a problem first — for example, 2x + 5 = 17.")
    problem = problem[:MAX_PROBLEM_CHARS]

    settings = get_settings()
    if settings.generator == "claude" and settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            resp = client.messages.create(
                model=settings.claude_model,
                max_tokens=1200,
                system=_SYSTEM,
                messages=[{"role": "user", "content":
                           f"Solve this math problem.\n\nPROBLEM:\n{problem}\n\n{_PROMPT}"}],
            )
            raw = "".join(getattr(b, "text", "") or "" for b in (resp.content or []))
            return _clean(_coerce(raw))
        except MathError:
            raise
        except Exception as e:
            raise MathError(f"The solver couldn't respond right now: {e}") from e
    return _offline(problem)


def teaser(steps: list) -> str:
    """The one-line peek a free user sees above the locked steps."""
    if not steps:
        return "See the full method, step by step."
    first = str(steps[0]).strip()
    if len(first) > 90:
        first = first[:87].rstrip() + "…"
    return first
