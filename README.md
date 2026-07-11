# StudyForge — Backend (Phase 1 MVP)

FastAPI backend that turns an uploaded file into a generated study kit
(summary + flashcards + multiple-choice quiz). This is the Phase 1 core of
the spec: the riskiest part (the upload → extract → generate pipeline)
built and tested first, before any mobile UI.

## What's built

- **Auth** — email/password signup + login, JWT bearer tokens.
- **Uploads** — PDF, plain text, and photo-of-notes (PNG/JPG via OCR).
  Server-side file-type (magic bytes) and size validation.
- **Async pipeline** — upload returns a job immediately; the client polls
  `GET /jobs/{id}`. Extraction and generation run in the background so the
  user never blocks on a long request.
- **Generation** — pluggable provider. `mock` (default, zero cost,
  deterministic) or `claude` (real Claude API). Every generated study set
  is validated against a strict schema before saving; malformed output is
  retried once, then the job fails with a clear error. Garbage is never saved.
- **Study sets + progress** — list/fetch study sets, record quiz scores.
- **Per-user isolation** — users can only read their own content.

## Run it

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# OCR needs the tesseract binary: `brew install tesseract` (mac) /
# `apt install tesseract-ocr` (linux)
cp .env.example .env          # edit if you like the defaults are fine for local
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for the interactive API.

## Switching on real AI generation

The pipeline runs on a mock generator by default so there are **no API
costs** during development. To use real Claude generation:

1. `pip install anthropic`
2. In `.env`: set `GENERATOR=claude` and `ANTHROPIC_API_KEY=sk-...`
3. Restart. Nothing else changes — the mock and Claude generators return
   the identical validated schema.

## Tests

```bash
python -m pytest
```

18 tests cover: PDF/text/image extraction (real sample files, real OCR),
schema-valid generation, the full upload→study-set flow for each input
type, auth, per-user isolation, and graceful failure on unsupported /
fake / corrupt files.

## API summary

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/signup` | Create account, returns token |
| POST | `/auth/login` | Log in, returns token |
| POST | `/uploads` | Upload a file, starts a job (202) |
| GET | `/jobs/{id}` | Poll job status |
| GET | `/study-sets` | List your study sets |
| GET | `/study-sets/{id}` | Full study set (summary, flashcards, quiz) |
| POST | `/study-sets/{id}/quiz/attempts` | Record a quiz score |
| GET | `/study-sets/{id}/quiz/attempts` | Score history |

## Known gaps / deferred (by design)

- Tables auto-create on startup; add **Alembic** migrations before production.
- Background jobs use FastAPI's in-process runner — fine for one server,
  move to a real queue (RQ/Celery/Arq) before scaling out.
- Local disk storage; swap `storage.py`'s write target for S3.
- Subscriptions/paywall, Apple/Google sign-in, and the Expo mobile app are
  **not** in this session — see the phase plan.
- Phase 2 input types (YouTube, audio/video transcription) not started.
