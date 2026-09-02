"""Math photo solver.

A student snaps a photo of a problem; we read it with vision AI and return
the answer plus one or more *methods* of getting there.

The free/paid split lives in `app/routers/math.py`, not here — this module's
job is to produce the richest correct result it can, and let the router
decide how much of it a given user is allowed to see.

Why the result is shaped as a list of methods
---------------------------------------------
Past a certain point in maths there is rarely one way to solve something.
A quadratic can be factored, completed, or run through the formula; a system
can be solved by substitution or elimination. Which one you reach for is
itself the skill, and it is the part a student most often has not been shown.
So every step carries two fields:

    do   — what you actually do on the page
    why  — why that move is legal, or why you'd pick it here

`do` is the solution. `why` is the understanding, and it is what Premium
buys. That mirrors how the category leader monetises: give away a complete,
genuinely useful answer, and charge for comprehension.

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

# A problem with more than a handful of genuinely distinct approaches is
# vanishingly rare, and a model asked for "as many as possible" will pad the
# list with restatements of the same method. Three is the honest ceiling.
MAX_METHODS = 3
MAX_STEPS_PER_METHOD = 8


class MathError(Exception):
    """User-facing solver problem (unreadable photo, AI/network)."""


_SYSTEM = (
    "You are an expert, patient math tutor reading a student's homework "
    "problem. You are meticulous and never guess: you re-check your "
    "arithmetic before answering. You always reply with valid JSON only — no "
    "prose outside the JSON, no markdown fences."
)

_PROMPT = (
    "Solve the math problem and explain how.\n\n"
    "Reply with ONLY a JSON object with exactly these keys:\n"
    '  "problem" — the problem exactly as written, in plain text (no LaTeX)\n'
    '  "answer"  — the final answer, as short as possible (e.g. "x = 7" or "24 cm²")\n'
    '  "topic"   — a 1-4 word label for the skill being practiced (e.g. "Quadratic equations")\n'
    '  "check"   — one short sentence showing how to verify the answer is right\n'
    '  "methods" — an array of 1 to 3 genuinely DIFFERENT ways to solve this\n\n'
    "Each entry in \"methods\" is an object with:\n"
    '  "name"    — the standard name of the technique (e.g. "Factoring", '
    '"Quadratic formula", "Completing the square", "Substitution", "Elimination", '
    '"Balancing both sides", "Order of operations")\n'
    '  "tagline" — under 60 characters on when you would choose this one '
    '(e.g. "Fastest when it factors cleanly", "Always works, even when it doesn\'t factor")\n'
    '  "steps"   — an array of 2-6 step objects, each with:\n'
    '        "do"  — what you actually do, showing the algebra. One clear sentence.\n'
    '        "why" — why that move is allowed, or why you\'d choose it here. '
    "One sentence, plain language, the bit a textbook usually leaves out.\n\n"
    "Rules for \"methods\":\n"
    "- Put the method you would actually teach first and the most niche last.\n"
    "- Only include a method if it genuinely applies to THIS problem and reaches "
    "the same answer. Do NOT pad the list — one real method beats three fake ones. "
    "If a technique doesn't work here (e.g. this quadratic doesn't factor over the "
    "integers), leave it out rather than forcing it.\n"
    "- The methods must be different approaches, not the same approach reworded.\n\n"
    "If there is no readable math problem, reply exactly: "
    '{"error": "no_problem"}\n'
    "If there are several problems, solve only the first/clearest one."
)

_IMAGE_PROMPT = "Read the math problem in this image. " + _PROMPT


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


def _clean_step(raw) -> Optional[dict]:
    """One step. A bare string is treated as `do` with no `why`."""
    if isinstance(raw, str):
        do, why = raw.strip(), ""
    elif isinstance(raw, dict):
        do = str(raw.get("do") or raw.get("step") or "").strip()
        why = str(raw.get("why") or "").strip()
    else:
        return None
    if not do:
        return None
    return {"do": do[:400], "why": why[:400]}


def _clean_method(raw, index: int) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    steps = raw.get("steps")
    if isinstance(steps, str):
        steps = [s for s in re.split(r"\n+", steps) if s.strip()]
    if not isinstance(steps, list):
        return None
    cleaned = [s for s in (_clean_step(s) for s in steps) if s][:MAX_STEPS_PER_METHOD]
    if not cleaned:
        return None
    name = str(raw.get("name") or "").strip()[:60] or (
        "The method" if index == 0 else f"Another way ({index + 1})")
    return {
        "name": name,
        "tagline": str(raw.get("tagline") or "").strip()[:90],
        "steps": cleaned,
    }


def _clean(data: dict) -> dict:
    if data.get("error") == "no_problem":
        raise MathError("I couldn't find a math problem in that photo. Make sure "
                        "the problem fills most of the frame and is in focus.")

    raw_methods = data.get("methods")
    methods: list[dict] = []
    if isinstance(raw_methods, list):
        for i, m in enumerate(raw_methods):
            cm = _clean_method(m, len(methods))
            if cm:
                methods.append(cm)
            if len(methods) >= MAX_METHODS:
                break

    # Older shape (and the offline solver): a flat list of step strings. Wrap it
    # as a single unnamed method rather than dropping the working entirely.
    if not methods and data.get("steps"):
        fallback = _clean_method({"name": "How to solve it", "steps": data["steps"]}, 0)
        if fallback:
            methods.append(fallback)

    # A model can return two "methods" that are the same approach reworded. That
    # reads as padding, and padding is exactly what makes a paid tier feel cheap.
    methods = _dedupe(methods)

    answer = str(data.get("answer") or "").strip()[:200]
    if not answer:
        raise MathError("The solver couldn't get a confident answer for that one. "
                        "Try a clearer photo, or type the problem out.")
    return {
        "problem": str(data.get("problem") or "").strip()[:MAX_PROBLEM_CHARS],
        "answer": answer,
        "methods": methods,
        "topic": str(data.get("topic") or "").strip()[:60],
        "check": str(data.get("check") or "").strip()[:300],
    }


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _dedupe(methods: list[dict]) -> list[dict]:
    """Drop methods that duplicate an earlier one by name or by working."""
    out: list[dict] = []
    seen_names: set[str] = set()
    seen_bodies: set[str] = set()
    for m in methods:
        name = _norm(m["name"])
        body = _norm(" ".join(s["do"] for s in m["steps"]))
        if name in seen_names or body in seen_bodies:
            continue
        seen_names.add(name)
        seen_bodies.add(body)
        out.append(m)
    return out


# ------------------------------------------------------------------ fallback

_SAFE_EXPR = re.compile(r"^[0-9+\-*/^(). \t]+$")
# At most one `**`, with a small integer exponent. Anything else is refused
# rather than evaluated.
_EXPONENT_OK = re.compile(r"[^*]*\*\*\s*\d{1,2}(?![\d*])[^*]*")


def _local_solve(text: str) -> Optional[dict]:
    """Solve plain arithmetic and simple `ax + b = c` without any AI.

    Used when no API key is set (dev/tests) and as a sanity net. It emits real
    `why` text — this path is what the test suite reads, so it has to exercise
    the same shape the AI path produces.
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
            prep = "from" if b > 0 else "to"
            steps.append({
                "do": f"{verb} {abs(b):g} {prep} both sides to get the x-term alone: "
                      f"{a:g}x = {c - b:g}.",
                "why": "Whatever you do to one side you must do to the other, so the "
                       "equation stays balanced and the answer doesn't change.",
            })
        if a != 1:
            steps.append({
                "do": f"Divide both sides by {a:g}: x = {c - b:g} ÷ {a:g}.",
                "why": f"x is being multiplied by {a:g}, and dividing is how you undo "
                       "multiplying — that leaves x on its own.",
            })
        steps.append({
            "do": f"So x = {xs}.",
            "why": "Once x stands alone on one side, whatever is on the other side "
                   "is the answer.",
        })
        return {
            "problem": t, "answer": f"x = {xs}", "topic": "Linear equations",
            "check": f"Put x = {xs} back in: it makes both sides equal {c:g}.",
            "methods": [{
                "name": "Balancing both sides",
                "tagline": "The standard way to undo a linear equation",
                "steps": steps,
            }],
        }

    # plain arithmetic
    expr = t.replace("^", "**").replace("×", "*").replace("÷", "/")
    # Exponentiation is the one operator here that can turn a tiny string into
    # an enormous computation: "9^9^9" pins a worker thread for minutes and eats
    # hundreds of MB, and every route in this app is sync, so a handful of those
    # takes the whole API down. Refuse anything with a big or stacked exponent.
    if "**" in expr and not _EXPONENT_OK.fullmatch(expr.replace(" ", "")):
        return None
    if _SAFE_EXPR.match(expr.replace("**", "^").replace("^", "")) or _SAFE_EXPR.match(expr):
        try:
            val = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 - regex-gated arithmetic only
            if isinstance(val, (int, float)):
                # f"{val:g}" raises OverflowError on a huge int (10**400), which
                # used to escape as a 500 on ordinary typed input.
                vs = f"{val:g}"
        except Exception:
            return None
        if isinstance(val, (int, float)):
            return {
                "problem": t, "answer": vs, "topic": "Order of operations",
                "check": f"Re-run the calculation in the same order — you should get {vs} again.",
                "methods": [{
                    "name": "Order of operations",
                    "tagline": "Brackets, powers, then × ÷, then + −",
                    "steps": [
                        {"do": "Work through it in order: brackets and powers first, "
                               "then multiplication and division, then addition and "
                               "subtraction.",
                         "why": "The order isn't a rule someone invented to be awkward — "
                                "it's what keeps everyone who reads the same expression "
                                "getting the same number."},
                        {"do": f"That gives {vs}.",
                         "why": "Nothing is left to simplify, so this is the value of "
                                "the whole expression."},
                    ],
                }],
            }
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

def _ask(client, settings, content) -> dict:
    resp = client.messages.create(
        # Always the sharper model: a wrong answer is worse than no answer,
        # and this is the feature people judge the whole app by.
        model=settings.claude_model,
        # Several methods with a `why` on every step is a lot more output than
        # the old single step list. Too low a ceiling truncates the JSON
        # mid-object and the whole solve fails to parse.
        max_tokens=3000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(getattr(b, "text", "") or "" for b in (resp.content or []))
    return _clean(_coerce(raw))


def solve_image(image_bytes: bytes, media_type: str) -> dict:
    """Solve a math problem from a photo. Returns answer/methods/topic/check."""
    if not image_bytes:
        raise MathError("No photo received. Try taking the picture again.")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise MathError("That photo is too large — try a smaller or lower-resolution shot.")

    settings = get_settings()
    if settings.generator == "claude" and settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            return _ask(client, settings, [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type,
                    "data": base64.b64encode(image_bytes).decode("ascii")}},
                {"type": "text", "text": _IMAGE_PROMPT},
            ])
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
            return _ask(client, settings,
                        f"Solve this math problem.\n\nPROBLEM:\n{problem}\n\n{_PROMPT}")
        except MathError:
            raise
        except Exception as e:
            raise MathError(f"The solver couldn't respond right now: {e}") from e
    return _offline(problem)


def teaser(text: str) -> str:
    """The one-line peek shown above a locked block."""
    first = str(text or "").strip()
    if not first:
        return "See why each step works."
    if len(first) > 90:
        first = first[:87].rstrip() + "…"
    return first
