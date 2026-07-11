#!/bin/bash
# StudyForge — double-click this file (on a Mac) to start the app.
# It sets everything up the first time, then opens StudyForge in your browser.

cd "$(dirname "$0")" || exit 1
echo "==============================================="
echo "   StudyForge — starting up"
echo "==============================================="

# 1. Make sure Python 3 exists.
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close..."
  exit 1
fi

# 2. Set up the virtual environment on first run.
if [ ! -d ".venv" ]; then
  echo "First-time setup (this takes a minute)…"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
fi

# Always make sure dependencies are present and up to date (fast after first run).
echo "Checking dependencies…"
./.venv/bin/pip install --quiet -r requirements.txt

# 3. Config file.
if [ ! -f ".env" ]; then cp .env.example .env; fi

# 3b. Stop any StudyForge server already running on this port, so a fresh
# launch always serves the latest code (closing the laptop lid doesn't stop it).
OLD_PIDS="$(lsof -ti tcp:8000 2>/dev/null)"
if [ -n "$OLD_PIDS" ]; then
  echo "Stopping a previous StudyForge server…"
  echo "$OLD_PIDS" | xargs kill 2>/dev/null
  sleep 1
  # Force-stop if it's still hanging around.
  STILL="$(lsof -ti tcp:8000 2>/dev/null)"
  if [ -n "$STILL" ]; then echo "$STILL" | xargs kill -9 2>/dev/null; sleep 1; fi
fi

# 4. Friendly note about OCR (photos of notes).
if ! command -v tesseract >/dev/null 2>&1; then
  echo "Note: photo-of-notes uploads need 'tesseract'. PDFs and text work without it."
  echo "      To enable photos later: install Homebrew, then run 'brew install tesseract'."
fi

# 5. Open the browser shortly after the server starts.
( sleep 2 && (open http://127.0.0.1:8000 2>/dev/null || xdg-open http://127.0.0.1:8000 2>/dev/null) ) &

echo ""
echo "StudyForge is running. Your browser should open automatically."
echo "If not, go to:  http://127.0.0.1:8000"
echo "To stop the app, close this window (or press Control-C)."
echo ""

./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
