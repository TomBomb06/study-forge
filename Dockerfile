# StudyForge backend — production container.
# Works on Railway, Fly.io, Render, or any Docker host.
FROM python:3.12-slim

# System deps: tesseract enables photo-of-notes OCR in production
# (no more "install tesseract" step — the server has it built in).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
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
