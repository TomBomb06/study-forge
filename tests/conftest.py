import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas

# Point config at throwaway locations BEFORE app modules import settings.
_TMP = tempfile.mkdtemp(prefix="studyforge-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["STORAGE_DIR"] = f"{_TMP}/storage"
os.environ["GENERATOR"] = "mock"
os.environ["SECRET_KEY"] = "test-secret"

SAMPLE_TEXT = (
    "Photosynthesis is the process by which green plants convert sunlight into "
    "chemical energy. Chlorophyll is the pigment that absorbs light energy. "
    "The light-dependent reactions occur in the thylakoid membranes. "
    "The Calvin cycle fixes carbon dioxide into glucose using ATP. "
    "Cellular respiration releases energy stored in glucose molecules. "
    "Mitochondria are the organelles where respiration primarily takes place. "
    "Oxygen is produced as a byproduct of photosynthesis and released into air. "
    "Stomata are pores in leaves that regulate gas exchange with the atmosphere."
)


@pytest.fixture(scope="session")
def sample_dir():
    d = os.path.join(os.path.dirname(__file__), "sample_files")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def sample_pdf(sample_dir):
    path = os.path.join(sample_dir, "notes.pdf")
    c = canvas.Canvas(path)
    y = 800
    for line in SAMPLE_TEXT.split(". "):
        c.drawString(50, y, line.strip() + ".")
        y -= 20
    c.save()
    return path


@pytest.fixture(scope="session")
def sample_txt(sample_dir):
    path = os.path.join(sample_dir, "notes.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(SAMPLE_TEXT)
    return path


@pytest.fixture(scope="session")
def sample_image(sample_dir):
    """A clearly-rendered note image so OCR has a fair shot."""
    path = os.path.join(sample_dir, "notes.png")
    img = Image.new("RGB", (900, 400), "white")
    draw = ImageDraw.Draw(img)
    lines = [
        "Photosynthesis converts sunlight into chemical energy.",
        "Chlorophyll is the pigment that absorbs light energy.",
        "The Calvin cycle fixes carbon dioxide into glucose.",
        "Mitochondria are where cellular respiration takes place.",
    ]
    y = 40
    for line in lines:
        draw.text((30, y), line, fill="black")
        y += 60
    img.save(path)
    return path


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def auth_headers(client):
    import uuid

    email = f"test-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
