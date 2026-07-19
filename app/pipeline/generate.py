"""Study-set generation providers.

Contract: generate(text, filename) -> StudySetContent (already validated).
Malformed output is retried once, then GenerationError is raised —
invalid content is never saved.

Switch providers with the GENERATOR env var: "mock" | "claude".
"""

import json
import re
from typing import Optional

from pydantic import ValidationError

from ..config import get_settings
from ..schemas import StudySetContent

MAX_INPUT_CHARS = 150_000  # ~ 50+ page doc; truncate beyond this for v1


class GenerationError(Exception):
    """User-facing generation failure."""


# ---------------------------------------------------------------- mock

_STOPWORDS = frozenset(
    "the a an and or but of to in on for with as by at from is are was were be "
    "been this that these those it its their there which who whom what when "
    "where how why not no can will would should could may might must have has "
    "had do does did if then than so such also into over under between".split()
)


def _sentences(text: str) -> list[str]:
    raw = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
    return [s.strip() for s in raw if len(s.strip()) >= 30]


def _key_term(sentence: str) -> Optional[str]:
    words = re.findall(r"[A-Za-z][A-Za-z\-']{3,}", sentence)
    candidates = [w for w in words if w.lower() not in _STOPWORDS]
    if not candidates:
        return None
    return max(candidates, key=len)


class MockGenerator:
    """Deterministic generator built from the source text itself.

    Produces schema-valid study sets with zero API cost — used for
    development and tests. Not pedagogically smart; that's Claude's job.
    """

    def generate(self, text: str, filename: str) -> StudySetContent:
        sents = _sentences(text)
        if len(sents) < 5:
            # Pad by splitting long sentences on commas as a fallback.
            extra = [p.strip() for s in sents for p in s.split(",") if len(p.strip()) >= 30]
            sents = list(dict.fromkeys(sents + extra))
        if len(sents) < 3:
            raise GenerationError(
                "Not enough substantive content to build a study set from this file."
            )

        title = re.sub(r"[_\-]+", " ", filename.rsplit(".", 1)[0]).strip().title() or "Study Set"
        summary = " ".join(sents[: min(8, len(sents))])
        if len(summary) < 50:
            summary = (summary + " " + text[:200]).strip()

        flashcards = []
        for s in sents:
            term = _key_term(s)
            if term:
                flashcards.append(
                    {"front": f"What is significant about '{term}'?", "back": s[:2000]}
                )
            if len(flashcards) >= 20:
                break
        while len(flashcards) < 5:
            i = len(flashcards)
            src = sents[i % len(sents)]
            flashcards.append(
                {"front": f"Key point #{i + 1} of this material?", "back": src[:2000]}
            )

        quiz = []
        for i, s in enumerate(sents):
            term = _key_term(s)
            if not term:
                continue
            blanked = re.sub(re.escape(term), "_____", s, count=1)
            distractors = []
            for other in sents:
                t = _key_term(other)
                if t and t.lower() != term.lower() and t not in distractors:
                    distractors.append(t)
                if len(distractors) == 3:
                    break
            while len(distractors) < 3:
                distractors.append(f"None of the above ({len(distractors) + 1})")
            choices = distractors[:]
            answer_index = i % 4
            choices.insert(answer_index, term)
            quiz.append(
                {
                    "question": f"Fill in the blank: {blanked[:900]}",
                    "choices": choices[:4] if answer_index < 4 else choices[1:5],
                    "answer_index": answer_index,
                    "explanation": s[:2000],
                }
            )
            if len(quiz) >= 10:
                break
        if len(quiz) < 3:
            raise GenerationError(
                "Not enough substantive content to build quiz questions from this file."
            )

        # Practice test: mix of true/false, fill-in-the-blank, short answer.
        test = []
        for i, s in enumerate(sents):
            term = _key_term(s)
            if not term:
                continue
            kind = ("true_false", "fill_blank", "short_answer")[i % 3]
            if kind == "true_false":
                test.append({"kind": "true_false", "question": f"True or false: {s[:900]}", "answer": "True"})
            elif kind == "fill_blank":
                blanked = re.sub(re.escape(term), "_____", s, count=1)
                test.append({"kind": "fill_blank", "question": blanked[:900], "answer": term})
            else:
                test.append({"kind": "short_answer", "question": f"Briefly explain: {term}", "answer": s[:900]})
            if len(test) >= 8:
                break
        while len(test) < 3:  # guarantee the schema minimum
            i = len(test)
            test.append({"kind": "short_answer",
                         "question": f"Summarize key point #{i + 1}.",
                         "answer": sents[i % len(sents)][:900]})

        # Matching game: term <-> definition pairs (reuse the flashcards).
        matching, seen = [], set()
        for c in flashcards:
            term = _key_term(c["back"]) or c["front"]
            if term.lower() in seen:
                continue
            seen.add(term.lower())
            matching.append({"term": term[:200], "definition": c["back"][:1000]})
            if len(matching) >= 8:
                break
        while len(matching) < 4:
            i = len(matching)
            matching.append({"term": f"Term {i + 1}", "definition": sents[i % len(sents)][:1000]})

        return StudySetContent(
            title=title[:255], summary=summary, flashcards=flashcards,
            quiz=quiz, test=test, matching=matching,
        )


# ---------------------------------------------------------------- claude

_SYSTEM = (
    "You are an expert tutor who turns raw source material into high-quality "
    "study kits. You write clear, accurate study notes and questions that test "
    "genuine understanding, not trivia. You only use information present in the "
    "source text and never invent facts. You always respond with a single valid "
    "JSON object and nothing else."
)

_PROMPT = """From the SOURCE TEXT below, produce a study kit as a JSON object with exactly these keys:

- "title": a short, specific title for this material (max ~60 chars).
- "summary": well-organized study notes covering the key concepts, definitions, and relationships. Use markdown (headings, bullet points, bold terms). Aim for thorough but concise — enough to revise from without the original.
- "flashcards": 8-12 objects, each {{"front": a question or term, "back": a clear, self-contained answer}}. Cover the most important concepts. Vary between definitions, cause/effect, and "why/how" cards.
- "quiz": 5-8 objects, each {{"question": str, "choices": [exactly 4 distinct strings], "answer_index": integer 0-3 pointing to the correct choice, "explanation": one sentence on why it's correct}}. Make distractors plausible, not obviously wrong. Test understanding, not just recall.
- "test": 4-6 objects, each {{"kind": one of "true_false" | "fill_blank" | "short_answer", "question": str, "answer": the correct answer as a string}}. Mix the kinds. For "true_false" the answer is "True" or "False"; for "fill_blank" put _____ in the question and the answer is the missing word/phrase.
- "matching": 4-6 objects, each {{"term": a key term, "definition": its matching definition}}. Used for a matching game, so keep terms and definitions short and unambiguous.

STRICT RULES:
- Respond with ONLY the JSON object. Start your reply with {{ and end with }}. No code fences, no commentary before or after.
- Keep the JSON complete and valid — close every bracket.
- Every answer must be supported by the source text.
- Each quiz question should have 4 choices and exactly one correct answer.

SOURCE TEXT:
{text}"""

# Match the first balanced {...} block, tolerating prose or fences around it.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fall back to the outermost brace block if the model added stray text.
        m = _JSON_BLOCK.search(raw)
        if not m:
            raise
        return json.loads(m.group(0))


_ALLOWED_KINDS = {"true_false", "fill_blank", "short_answer"}


def _coerce(data: dict) -> dict:
    """Make real-model output conform to the schema instead of rejecting it.

    Drops malformed items, fixes types, clamps quiz answers, and normalizes
    test kinds — so a good-but-slightly-off Claude response still yields a
    usable study set rather than a hard failure.
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    out["title"] = (str(out.get("title") or "Study Set").strip() or "Study Set")[:255]
    out["summary"] = str(out.get("summary") or "").strip() or "Study notes."

    flashcards = []
    for c in out.get("flashcards") or []:
        if isinstance(c, dict) and str(c.get("front", "")).strip() and str(c.get("back", "")).strip():
            flashcards.append({"front": str(c["front"])[:500], "back": str(c["back"])[:2000]})
    out["flashcards"] = flashcards

    quiz = []
    for q in out.get("quiz") or []:
        if not isinstance(q, dict):
            continue
        choices = [str(x) for x in (q.get("choices") or []) if str(x).strip()][:6]
        if len(choices) < 2:
            continue
        try:
            ai = int(q.get("answer_index", 0))
        except (TypeError, ValueError):
            ai = 0
        if ai < 0 or ai >= len(choices):
            ai = 0
        quiz.append({
            "question": (str(q.get("question", "")).strip() or "Question")[:1000],
            "choices": choices,
            "answer_index": ai,
            "explanation": str(q.get("explanation", "") or "")[:2000],
        })
    out["quiz"] = quiz

    test = []
    for t in out.get("test") or []:
        if not isinstance(t, dict):
            continue
        kind = str(t.get("kind", "short_answer")).lower().strip()
        if kind not in _ALLOWED_KINDS:
            kind = "short_answer"
        question, answer = str(t.get("question", "")).strip(), str(t.get("answer", "")).strip()
        if question and answer:
            test.append({"kind": kind, "question": question[:1000], "answer": answer[:1000]})
    out["test"] = test

    matching = []
    for m in out.get("matching") or []:
        if isinstance(m, dict) and str(m.get("term", "")).strip() and str(m.get("definition", "")).strip():
            matching.append({"term": str(m["term"])[:200], "definition": str(m["definition"])[:1000]})
    out["matching"] = matching
    return out


class ClaudeGenerator:
    def __init__(self, client=None, model: Optional[str] = None) -> None:
        settings = get_settings()
        self._model = model or settings.claude_model
        if client is not None:  # injected (e.g. in tests)
            self._client = client
            return
        if not settings.anthropic_api_key:
            raise GenerationError(
                "GENERATOR=claude but ANTHROPIC_API_KEY is not set. "
                "Add your key to backend/.env."
            )
        try:
            import anthropic
        except ImportError:
            raise GenerationError(
                "The 'anthropic' package is not installed. Run: pip install anthropic"
            )
        try:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        except Exception as e:  # bad key format, etc.
            raise GenerationError(f"Could not initialize the Claude client: {e}")

    def generate(self, text: str, filename: str) -> StudySetContent:
        last_error: Optional[Exception] = None
        last_raw: str = ""
        for _attempt in range(2):  # retry malformed output once, then fail
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=16000,
                    system=_SYSTEM,
                    messages=[
                        {"role": "user", "content": _PROMPT.format(text=text)}
                    ],
                )
                # Read every text block (some models return multiple blocks).
                raw = "".join(
                    getattr(b, "text", "") or "" for b in (response.content or [])
                ).strip()
                last_raw = raw
                return StudySetContent.model_validate(_coerce(_extract_json(raw)))
            except (json.JSONDecodeError, ValidationError, IndexError, AttributeError, TypeError) as e:
                last_error = e
                continue
            except GenerationError:
                raise
            except Exception as e:
                # Network/auth/rate-limit errors from the SDK — don't retry blindly.
                raise GenerationError(
                    f"The AI service returned an error: {e}"
                ) from e
        # Log the underlying reason server-side; keep the user-facing message clean.
        detail = f"{type(last_error).__name__}: {str(last_error)[:250]}"
        head = last_raw[:200].replace("\n", " ")
        print(f"[generate] malformed AI output after retry — {detail} | head: {head!r}")
        raise GenerationError(
            "The AI had trouble with this material. Please try again — "
            "it usually works on the second attempt."
        ) from last_error


# ---------------------------------------------------------------- entry

def get_generator():
    settings = get_settings()
    if settings.generator == "claude":
        return ClaudeGenerator()
    return MockGenerator()


def generate_study_set(text: str, filename: str) -> StudySetContent:
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]
    content = get_generator().generate(text, filename)
    # Belt-and-braces: whatever the provider, re-validate before returning.
    return StudySetContent.model_validate(content.model_dump())
