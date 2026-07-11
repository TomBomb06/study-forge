"""Integration tests: full upload -> job -> study set flow per input type,
plus auth and validation edge cases."""

import io
import time


def _wait_for_job(client, headers, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=headers)
        assert r.status_code == 200, r.text
        status = r.json()["status"]
        if status in ("completed", "failed"):
            return r.json()
        time.sleep(0.1)
    raise AssertionError("Job did not finish in time")


def _upload(client, headers, path, filename, content_type):
    with open(path, "rb") as f:
        return client.post(
            "/uploads",
            headers=headers,
            files={"file": (filename, f, content_type)},
        )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_full_flow_pdf(client, auth_headers, sample_pdf):
    r = _upload(client, auth_headers, sample_pdf, "notes.pdf", "application/pdf")
    assert r.status_code == 202, r.text
    job = _wait_for_job(client, auth_headers, r.json()["id"])
    assert job["status"] == "completed", job.get("error")

    ss = client.get(f"/study-sets/{job['study_set_id']}", headers=auth_headers).json()
    assert len(ss["flashcards"]) >= 5
    assert len(ss["quiz"]) >= 3
    assert len(ss["test"]) >= 3
    assert len(ss["matching"]) >= 4
    assert ss["summary"]


def test_full_flow_txt(client, auth_headers, sample_txt):
    r = _upload(client, auth_headers, sample_txt, "notes.txt", "text/plain")
    job = _wait_for_job(client, auth_headers, r.json()["id"])
    assert job["status"] == "completed", job.get("error")


def test_full_flow_image(client, auth_headers, sample_image):
    r = _upload(client, auth_headers, sample_image, "notes.png", "image/png")
    job = _wait_for_job(client, auth_headers, r.json()["id"])
    assert job["status"] == "completed", job.get("error")


def test_upload_requires_auth(client, sample_txt):
    r = _upload(client, {}, sample_txt, "notes.txt", "text/plain")
    assert r.status_code == 401


def test_rejects_unsupported_type(client, auth_headers):
    r = client.post(
        "/uploads",
        headers=auth_headers,
        files={"file": ("evil.exe", io.BytesIO(b"MZ..."), "application/octet-stream")},
    )
    assert r.status_code == 422
    assert "Unsupported" in r.json()["detail"]


def test_rejects_fake_pdf(client, auth_headers):
    # .pdf extension but not actually a PDF -> magic-byte check must catch it.
    r = client.post(
        "/uploads",
        headers=auth_headers,
        files={"file": ("fake.pdf", io.BytesIO(b"not a real pdf"), "application/pdf")},
    )
    assert r.status_code == 422


def test_unreadable_pdf_fails_gracefully(client, auth_headers):
    # Valid PDF signature but garbage body -> job should fail, not 500.
    body = b"%PDF-1.4\ngarbage that is not a real pdf structure\n%%EOF"
    r = client.post(
        "/uploads",
        headers=auth_headers,
        files={"file": ("broken.pdf", io.BytesIO(body), "application/pdf")},
    )
    assert r.status_code == 202
    job = _wait_for_job(client, auth_headers, r.json()["id"])
    assert job["status"] == "failed"
    assert job["error"]


def test_cannot_read_another_users_study_set(client, auth_headers, sample_txt):
    r = _upload(client, auth_headers, sample_txt, "notes.txt", "text/plain")
    job = _wait_for_job(client, auth_headers, r.json()["id"])
    ss_id = job["study_set_id"]

    other = client.post(
        "/auth/signup", json={"email": "other@example.com", "password": "password123"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get(f"/study-sets/{ss_id}", headers=other_headers).status_code == 404


def test_quiz_attempt_tracking(client, auth_headers, sample_txt):
    r = _upload(client, auth_headers, sample_txt, "notes.txt", "text/plain")
    job = _wait_for_job(client, auth_headers, r.json()["id"])
    ss_id = job["study_set_id"]

    r = client.post(
        f"/study-sets/{ss_id}/quiz/attempts",
        headers=auth_headers,
        json={"score": 4, "total": 5},
    )
    assert r.status_code == 201
    attempts = client.get(
        f"/study-sets/{ss_id}/quiz/attempts", headers=auth_headers
    ).json()
    assert len(attempts) == 1 and attempts[0]["score"] == 4


def test_duplicate_signup_rejected(client):
    client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    r = client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    assert r.status_code == 409


# ---------- Non-file inputs: paste text and web link ----------

PASTED = (
    "The mitochondria is the powerhouse of the cell, producing ATP through "
    "cellular respiration. Ribosomes synthesize proteins from amino acids. "
    "The nucleus stores genetic information in the form of DNA. The endoplasmic "
    "reticulum transports materials throughout the cell. The Golgi apparatus "
    "packages and ships proteins to their destinations."
)


def test_paste_text_flow(client, auth_headers):
    r = client.post(
        "/uploads/text",
        headers=auth_headers,
        json={"content": PASTED, "title": "Cell Biology"},
    )
    assert r.status_code == 202, r.text
    job = _wait_for_job(client, auth_headers, r.json()["id"])
    assert job["status"] == "completed", job.get("error")
    ss = client.get(f"/study-sets/{job['study_set_id']}", headers=auth_headers).json()
    assert ss["title"] == "Cell Biology"
    assert len(ss["flashcards"]) >= 5


def test_paste_text_too_short_fails(client, auth_headers):
    r = client.post(
        "/uploads/text", headers=auth_headers, json={"content": "too short"}
    )
    assert r.status_code == 202
    job = _wait_for_job(client, auth_headers, r.json()["id"])
    assert job["status"] == "failed"


def test_link_flow_mocked(client, auth_headers, monkeypatch):
    # Mock the network fetch so no real HTTP happens.
    from app.pipeline import jobs

    monkeypatch.setattr(
        jobs, "fetch_url_text", lambda url: ("Cell Biology Article", PASTED)
    )
    r = client.post(
        "/uploads/link",
        headers=auth_headers,
        json={"url": "https://example.com/cells"},
    )
    assert r.status_code == 202
    job = _wait_for_job(client, auth_headers, r.json()["id"])
    assert job["status"] == "completed", job.get("error")
    ss = client.get(f"/study-sets/{job['study_set_id']}", headers=auth_headers).json()
    assert len(ss["quiz"]) >= 3


def test_link_requires_auth(client):
    r = client.post("/uploads/link", json={"url": "https://example.com"})
    assert r.status_code == 401


def test_paste_text_requires_auth(client):
    r = client.post("/uploads/text", json={"content": PASTED})
    assert r.status_code == 401
