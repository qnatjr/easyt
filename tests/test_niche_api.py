"""The niche bank's HTTP surface, including how a niche reaches a project."""

import pytest

from app.services import niche as niche_service


@pytest.fixture(autouse=True)
def no_real_jobs(monkeypatch):
    """Analysis is exercised in test_niche_job; here only the wiring matters."""

    async def noop(niche_id):
        return None

    monkeypatch.setattr(niche_service, "run_job", noop)


def make_niche(client, name="Cold-open history"):
    response = client.post("/api/niches", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


# --- CRUD -------------------------------------------------------------------


def test_creating_a_niche_returns_it_idle(client):
    doc = make_niche(client)

    assert doc["name"] == "Cold-open history"
    assert doc["analysis"]["status"] == "idle"
    assert doc["profile"] == {}


def test_a_blank_name_is_rejected(client):
    assert client.post("/api/niches", json={"name": "   "}).status_code == 422


def test_niches_are_listed(client):
    made = make_niche(client)

    listing = client.get("/api/niches").json()["niches"]

    assert [n["id"] for n in listing] == [made["id"]]
    assert listing[0]["shot_count"] == 0


def test_a_niche_can_be_edited(client):
    doc = make_niche(client)

    updated = client.patch(
        f"/api/niches/{doc['id']}",
        json={
            "name": "Renamed",
            "notes": "worth remembering",
            "transcript": "pasted narration",
            "youtube_url": "https://youtu.be/x",
        },
    ).json()

    assert updated["name"] == "Renamed"
    assert updated["notes"] == "worth remembering"
    assert updated["transcript"] == "pasted narration"
    assert updated["source"]["youtube_url"] == "https://youtu.be/x"


def test_a_niche_can_be_deleted(client):
    doc = make_niche(client)

    assert client.delete(f"/api/niches/{doc['id']}").status_code == 204
    assert client.get(f"/api/niches/{doc['id']}").status_code == 404


def test_a_traversing_niche_id_is_refused(client):
    assert client.get("/api/niches/..").status_code in (400, 404)


# --- Screenshots ------------------------------------------------------------


def test_screenshots_are_stored_and_listed(client):
    doc = make_niche(client)

    response = client.post(
        f"/api/niches/{doc['id']}/shots",
        files=[
            ("files", ("a.png", PNG, "image/png")),
            ("files", ("b.png", PNG, "image/png")),
        ],
    )

    assert response.status_code == 201, response.text
    assert len(response.json()["shots"]) == 2


def test_an_unsupported_image_type_names_what_is_allowed(client):
    doc = make_niche(client)

    response = client.post(
        f"/api/niches/{doc['id']}/shots",
        files=[("files", ("a.tiff", b"II*\x00", "image/tiff"))],
    )

    assert response.status_code == 400
    # The allow-list is the API's own: bmp/gif/png/jpeg/webp.
    assert "webp" in response.json()["detail"]


def test_a_stored_screenshot_can_be_fetched_back(client):
    doc = make_niche(client)
    shot = client.post(
        f"/api/niches/{doc['id']}/shots", files=[("files", ("a.png", PNG, "image/png"))]
    ).json()["shots"][0]

    response = client.get(f"/api/niches/{doc['id']}/shots/{shot['filename']}")

    assert response.status_code == 200
    assert response.content == PNG


def test_a_traversing_screenshot_name_is_refused(client):
    doc = make_niche(client)

    response = client.get(f"/api/niches/{doc['id']}/shots/..%2F..%2Fniche.json")

    assert response.status_code == 404


def test_a_screenshot_can_be_removed(client):
    doc = make_niche(client)
    shot = client.post(
        f"/api/niches/{doc['id']}/shots", files=[("files", ("a.png", PNG, "image/png"))]
    ).json()["shots"][0]

    assert (
        client.delete(f"/api/niches/{doc['id']}/shots/{shot['filename']}").status_code
        == 200
    )
    assert client.get(f"/api/niches/{doc['id']}").json()["shots"] == []


# --- Analysis ---------------------------------------------------------------


def test_analysis_is_queued_not_awaited(client):
    doc = make_niche(client)
    client.patch(f"/api/niches/{doc['id']}", json={"transcript": "something to chew on"})

    response = client.post(f"/api/niches/{doc['id']}/analyse")

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_analysing_an_empty_niche_is_refused_before_it_starts(client):
    doc = make_niche(client)

    response = client.post(f"/api/niches/{doc['id']}/analyse")

    assert response.status_code == 400


def test_a_run_interrupted_by_a_restart_is_reported_as_such(client):
    # "running" is persisted but the job only ever lived in the old process.
    # Left alone the browser would poll forever.
    from app.storage import update_niche

    doc = make_niche(client)
    update_niche(doc["id"], lambda d: d.update({"analysis": {"status": "running"}}))

    state = client.get(f"/api/niches/{doc['id']}/status").json()

    assert state["status"] == "interrupted"
    assert state["running"] is False


# --- Reaching a project -----------------------------------------------------


PROFILE = {
    "hook_patterns": ["opens on a hard number"],
    "tone": "wry",
    "pacing": "short sentences",
    "vocabulary": ["genuinely"],
    "structure": ["scale first"],
    "title_style": "a number plus a stake",
    "visual_style": "cold blues",
    "shot_language": ["tight on hands"],
    "avoid": ["stock footage"],
    "image_prompt_seed": "documentary stills",
}


def analysed_niche(client, profile=None):
    from app.storage import update_niche

    doc = make_niche(client)
    update_niche(
        doc["id"],
        lambda d: d.update(
            {"profile": profile or PROFILE, "analysis": {"status": "done"}}
        ),
    )
    return doc


def test_a_project_created_with_a_niche_carries_its_profile(client):
    niche = analysed_niche(client)

    project = client.post(
        "/api/projects", json={"topic": "Rome", "niche_id": niche["id"]}
    ).json()

    assert project["context"]["niche_id"] == niche["id"]
    assert project["context"]["niche_profile"]["tone"] == "wry"
    assert project["context"]["niche_name"] == "Cold-open history"


def test_a_project_created_without_a_niche_carries_no_profile(client):
    project = client.post("/api/projects", json={"topic": "Rome"}).json()

    assert project["context"].get("niche_profile") in (None, {})


def test_an_unknown_niche_is_a_404_rather_than_a_silent_miss(client):
    response = client.post(
        "/api/projects", json={"topic": "Rome", "niche_id": "no-such-niche"}
    )

    assert response.status_code == 404


def test_editing_the_niche_later_does_not_rewrite_an_existing_project(client):
    # The profile is snapshotted deliberately: re-analysing a niche must not
    # silently change the reference a finished project was written against.
    from app.storage import update_niche

    niche = analysed_niche(client)
    project = client.post(
        "/api/projects", json={"topic": "Rome", "niche_id": niche["id"]}
    ).json()

    update_niche(
        niche["id"], lambda d: d.update({"profile": {**PROFILE, "tone": "changed"}})
    )

    reloaded = client.get(f"/api/projects/{project['id']}").json()
    assert reloaded["context"]["niche_profile"]["tone"] == "wry"


def test_a_project_can_adopt_a_niche_after_the_fact(client):
    niche = analysed_niche(client)
    project = client.post("/api/projects", json={"topic": "Rome"}).json()

    updated = client.patch(
        f"/api/projects/{project['id']}", json={"niche_id": niche["id"]}
    ).json()

    assert updated["context"]["niche_profile"]["tone"] == "wry"


def test_a_project_can_drop_its_niche(client):
    niche = analysed_niche(client)
    project = client.post(
        "/api/projects", json={"topic": "Rome", "niche_id": niche["id"]}
    ).json()

    updated = client.patch(f"/api/projects/{project['id']}", json={"niche_id": ""}).json()

    assert updated["context"]["niche_id"] == ""
    assert updated["context"]["niche_profile"] == {}


def test_the_niches_page_renders(client):
    response = client.get("/niches")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
