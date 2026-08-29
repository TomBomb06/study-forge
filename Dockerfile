# StudyForge backend — production container.
# Works on Railway, Fly.io, Render, or any Docker host.
FROM python:3.12-slim

# System deps:
#   tesseract — photo-of-notes OCR in production
#   ffmpeg    — compresses and splits long lecture recordings so they fit
#               under the transcription API's 25 MB per-file ceiling
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir openai

COPY . .

# Uploaded files live here; mount a persistent volume at this path in prod.
ENV STORAGE_DIR=/data/storage
RUN mkdir -p /data/storage

# Hosts inject $PORT; default to 8000 for local `docker run`.
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
