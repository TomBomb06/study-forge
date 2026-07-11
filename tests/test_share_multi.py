"""Tests for shareable sets (share/preview/import) and multi-file upload."""

import io
import time


def _make_set(client, headers, title="Bio"):
    text = (
        "Photosynthesis is the process by which green plants convert sunlight into "
        "chemical energy. Chlorophyll is the pigment that absorbs light energy. "
        "The Calvin cycle fixes carbon dioxide into glucose using ATP. "
        "Cellular respiration releases energy stored in glucose. Mitochondria host it. "
        "Oxygen is produced as a byproduct. Stomata regulate gas exchange in leaves."
    )
    r = client.post("/uploads/text", headers=headers, json={"content": text, "title": title})
    jid = r.json()["id"]
    for _ in range(50):
        j = client.get(f"/jobs/{jid}", headers=headers).json()
        if j["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "completed"
    return j["study_set_id"]


def _second_user(client):
    import uuid
    email = f"friend-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/signup", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_share_preview_and_import(client, auth_headers):
    ss_id = _make_set(client, auth_headers)
    token = client.post(f"/study-sets/{ss_id}/share", headers=auth_headers).json()["token"]
    assert token

    # Preview is public (no auth needed).
    preview = client.get(f"/shared/{token}").json()
    assert preview["title"] == "Bio"
    assert preview["counts"]["flashcards"] >= 5

    # A different user imports it into their library.
    friend = _second_user(client)
    r = client.post(f"/shared/{token}/import", headers=friend)
    assert r.status_code == 201
    new_id = r.json()["id"]
    ss = client.get(f"/study-sets/{new_id}", headers=friend).json()
    assert len(ss["flashcards"]) >= 5
    assert ss["source_filename"].startswith("Imported")


def test_share_token_is_stable(client, auth_headers):
    ss_id = _make_set(client, auth_headers)
    t1 = client.post(f"/study-sets/{ss_id}/share", headers=auth_headers).json()["token"]
    t2 = client.post(f"/study-sets/{ss_id}/share", headers=auth_headers).json()["token"]
    assert t1 == t2  # sharing twice returns the same link


def test_cannot_import_own_set(client, auth_headers):
    ss_id = _make_set(client, auth_headers)
    token = client.post(f"/study-sets/{ss_id}/share", headers=auth_headers).json()["token"]
    assert client.post(f"/shared/{token}/import", headers=auth_headers).status_code == 409


def test_unknown_share_token_404(client):
    assert client.get("/shared/nope").status_code == 404


def test_multi_file_combines(client, auth_headers):
    a = (b"Newton's first law states that an object in motion stays in motion unless a force acts on it. "
         b"This property is called inertia. Momentum is conserved in a closed system. "
         b"A force is a push or pull that can change an object's motion.")
    b = (b"Newton's second law says that force equals mass times acceleration. "
         b"Heavier objects need more force to accelerate. The third law states that every action "
         b"has an equal and opposite reaction. Friction is a force that opposes motion between surfaces.")
    files = [
        ("files", ("one.txt", io.BytesIO(a), "text/plain")),
        ("files", ("two.txt", io.BytesIO(b), "text/plain")),
    ]
    r = client.post("/uploads/multi", headers=auth_headers, files=files, data={"title": "Physics combined"})
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    for _ in range(50):
        j = client.get(f"/jobs/{jid}", headers=auth_headers).json()
        if j["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "completed", j.get("error")
    ss = client.get(f"/study-sets/{j['study_set_id']}", headers=auth_headers).json()
    assert ss["title"].lower() == "physics combined"
    # Content from both files should be present.
    blob = (ss["summary"] + " " + " ".join(c["back"] for c in ss["flashcards"])).lower()
    assert "inertia" in blob or "acceleration" in blob


def test_multi_requires_auth(client):
    files = [("files", ("x.txt", io.BytesIO(b"hello world this is text"), "text/plain"))]
    assert client.post("/uploads/multi", files=files).status_code == 401
