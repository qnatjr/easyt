import pytest
from fastapi import HTTPException

from app.storage import (
    delete_project,
    list_projects,
    load_project,
    new_project,
    project_dir,
    save_project,
    slugify,
)

CONTEXT = {
    "topic": "The Fall of Rome",
    "category": "history",
    "language": "en",
    "reference": "",
    "minutes": 5.0,
    "wpm": 150,
    "max_words": 825,
    "target_words": 750,
}


def test_slugify():
    assert slugify("The Fall of Rome") == "the-fall-of-rome"
    assert slugify("  Multiple   spaces & symbols!! ") == "multiple-spaces-symbols"
    assert slugify("") == "project"
    assert slugify("日本語だけ") == "project"
    assert len(slugify("x" * 200)) <= 40


def test_create_and_load():
    doc = new_project(dict(CONTEXT))

    assert doc["id"].startswith("the-fall-of-rome-")
    assert doc["step"] == 1
    assert doc["context"]["topic"] == "The Fall of Rome"
    assert (project_dir(doc["id"]) / "audio").is_dir()

    assert load_project(doc["id"])["id"] == doc["id"]


def test_save_round_trip_bumps_updated_at():
    doc = new_project(dict(CONTEXT))
    before = doc["updated_at"]

    doc["script"] = "some words"
    save_project(doc)

    reloaded = load_project(doc["id"])
    assert reloaded["script"] == "some words"
    assert reloaded["updated_at"] >= before


def test_missing_project_is_a_404():
    with pytest.raises(HTTPException) as exc:
        load_project("does-not-exist")
    assert exc.value.status_code == 404


def test_path_traversal_is_rejected():
    # ".." is the dangerous one: Path("..").name is ".." too, so a round-trip
    # basename check lets it through and the directory resolves to the data
    # root — one DELETE would take out every project and the whole voice bank.
    for bad in ("..", ".", "...", "../escape", "a/b", "/etc", "", "..%2f", "A-B", "-x"):
        with pytest.raises(HTTPException) as exc:
            project_dir(bad)
        assert exc.value.status_code == 400, bad


def test_generated_ids_are_accepted():
    doc = new_project(dict(CONTEXT))
    assert project_dir(doc["id"]).name == doc["id"]


def test_list_summarises_and_sorts_newest_first():
    first = new_project(dict(CONTEXT))
    second = new_project({**CONTEXT, "topic": "Second Topic"})
    second["script"] = "words"
    second["scenes"] = [{"index": 1}]
    save_project(second)

    listing = list_projects()
    ids = [p["id"] for p in listing]

    assert set(ids) == {first["id"], second["id"]}
    entry = next(p for p in listing if p["id"] == second["id"])
    assert entry["has_script"] is True
    assert entry["scene_count"] == 1
    assert entry["topic"] == "Second Topic"


def test_update_project_does_not_lose_concurrent_writes():
    """Requests run in a thread pool while the voice-over job writes too."""
    import threading
    import time

    from app.storage import update_project

    pid = new_project(dict(CONTEXT))["id"]

    def bump() -> None:
        def mutate(doc: dict) -> None:
            current = doc.get("counter", 0)
            # Widen the read-modify-write window so an unlocked version would
            # reliably lose updates rather than passing by luck.
            time.sleep(0.01)
            doc["counter"] = current + 1

        update_project(pid, mutate)

    threads = [threading.Thread(target=bump) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert load_project(pid)["counter"] == 10


def test_delete_removes_the_directory():
    doc = new_project(dict(CONTEXT))
    delete_project(doc["id"])

    assert not project_dir(doc["id"]).exists()
    with pytest.raises(HTTPException):
        delete_project(doc["id"])


# --- Niches -----------------------------------------------------------------


def test_new_niche_starts_idle_with_an_empty_profile():
    from app.storage import new_niche, niche_dir

    doc = new_niche("Cold-open history")

    assert doc["id"].startswith("cold-open-history-")
    assert doc["name"] == "Cold-open history"
    assert doc["analysis"]["status"] == "idle"
    assert doc["profile"] == {}
    assert doc["shots"] == []
    assert (niche_dir(doc["id"]) / "shots").is_dir()


def test_niche_round_trip():
    from app.storage import load_niche, new_niche, save_niche

    doc = new_niche("Cold-open history")
    doc["transcript"] = "some narration"
    save_niche(doc)

    assert load_niche(doc["id"])["transcript"] == "some narration"


def test_missing_niche_is_a_404():
    from app.storage import load_niche

    with pytest.raises(HTTPException) as exc:
        load_niche("does-not-exist")
    assert exc.value.status_code == 404


def test_niche_path_traversal_is_rejected():
    from app.storage import niche_dir

    for bad in ("..", ".", "...", "../escape", "a/b", "/etc", "", "..%2f", "A-B", "-x"):
        with pytest.raises(HTTPException) as exc:
            niche_dir(bad)
        assert exc.value.status_code == 400, bad


def test_listing_niches_summarises_and_sorts_newest_first():
    from app.storage import list_niches, new_niche, save_niche

    first = new_niche("First niche")
    second = new_niche("Second niche")
    second["profile"] = {"tone": "wry"}
    second["shots"] = [{"filename": "a.png", "note": ""}]
    second["analysis"] = {"status": "done"}
    save_niche(second)

    listing = list_niches()
    entry = next(n for n in listing if n["id"] == second["id"])

    assert {n["id"] for n in listing} == {first["id"], second["id"]}
    assert entry["name"] == "Second niche"
    assert entry["shot_count"] == 1
    assert entry["has_profile"] is True
    assert entry["status"] == "done"
    assert listing[0]["id"] == second["id"]


def test_deleting_a_niche_removes_its_directory():
    from app.storage import delete_niche, new_niche, niche_dir

    doc = new_niche("Doomed")
    delete_niche(doc["id"])

    assert not niche_dir(doc["id"]).exists()
    with pytest.raises(HTTPException):
        delete_niche(doc["id"])


def test_update_niche_serialises_concurrent_writes():
    """The analysis job writes clip results while the browser PATCHes notes."""
    import threading
    import time

    from app.storage import load_niche, new_niche, update_niche

    nid = new_niche("Contended")["id"]

    def bump() -> None:
        def mutate(doc: dict) -> None:
            current = doc.get("counter", 0)
            time.sleep(0.01)
            doc["counter"] = current + 1

        update_niche(nid, mutate)

    threads = [threading.Thread(target=bump) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert load_niche(nid)["counter"] == 10
