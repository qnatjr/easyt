"""End-to-end walk through the wizard with MIMO mocked out."""

import csv
import io

import pytest

from app.services import scenes as scenes_service
from app.services import script as script_service
from app.services import titles as titles_service
from app.services import voiceover as voiceover_service

from tests.conftest import make_wav

NEW_PROJECT = {
    "topic": "Why the Roman aqueducts still stand",
    "category": "history",
    "language": "en",
    "reference": "",
    "minutes": 1.0,
    "wpm": 150,
}


@pytest.fixture
def mimo(monkeypatch):
    """Deterministic stand-ins for every MIMO call the API can make."""

    def titles(*a, **k):
        return {
            "titles": [
                {"title": f"Title {i}", "ctr_score": 90 - i * 10, "hooks": ["gap"], "reason": "r"}
                for i in range(3)
            ]
        }

    def script(*a, **k):
        sentences = [f"Sentence number {i} carries the story forward." for i in range(20)]
        return {"script": " ".join(sentences)}

    def scene_json(system, user, **kwargs):
        # Order matters: the batch prompt embeds the style bible, so match on
        # strings unique to each stage rather than on "visual system".
        if "Define the visual system" in user:
            return {"art_direction": "grainy 16mm", "palette": "amber and slate"}
        if "thumbnail_prompt" in user:
            return {"thumbnail_prompt": "one bold face, extreme contrast"}
        # A prompt batch: echo back an entry per index it was given.
        indices = [
            int(line.split(".", 1)[0])
            for line in user.splitlines()
            if line.strip() and line.split(".", 1)[0].strip().isdigit()
        ]
        return {
            "scenes": [
                {"index": i, "image_prompt": f"image {i}", "focus": f"focus {i}"}
                for i in indices
            ]
        }

    monkeypatch.setattr(titles_service, "chat_json", titles)
    monkeypatch.setattr(script_service, "chat_json", script)
    monkeypatch.setattr(scenes_service, "chat_json", scene_json)
    monkeypatch.setattr(voiceover_service, "synth_clone", lambda *a, **k: make_wav(0.5))


def make_project(client) -> str:
    return client.post("/api/projects", json=NEW_PROJECT).json()["id"]


def make_voice(client) -> str:
    response = client.post(
        "/api/voices",
        files={"file": ("sample.wav", make_wav(1.0), "audio/wav")},
        data={"name": "Narrator", "category": "documentary", "stability": "70"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- Pages ------------------------------------------------------------------


def test_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/voices").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_project_page_renders(client):
    pid = make_project(client)
    response = client.get(f"/projects/{pid}")

    assert response.status_code == 200
    assert pid in response.text


def test_unknown_project_page_is_a_404(client):
    assert client.get("/projects/nope").status_code == 404


# --- Project CRUD -----------------------------------------------------------


def test_create_derives_the_word_budget(client):
    doc = client.post("/api/projects", json=NEW_PROJECT).json()

    assert doc["context"]["target_words"] == 150
    assert doc["context"]["max_words"] == 165
    assert doc["context"]["scene_seconds"] == 3
    assert doc["step"] == 1


def test_scene_cadence_is_configurable(client):
    doc = client.post("/api/projects", json={**NEW_PROJECT, "scene_seconds": 2}).json()
    assert doc["context"]["scene_seconds"] == 2

    assert (
        client.post("/api/projects", json={**NEW_PROJECT, "scene_seconds": 1}).status_code == 422
    )
    assert (
        client.post("/api/projects", json={**NEW_PROJECT, "scene_seconds": 30}).status_code == 422
    )


def test_create_rejects_an_empty_topic(client):
    assert client.post("/api/projects", json={**NEW_PROJECT, "topic": ""}).status_code == 422


def test_create_rejects_an_absurd_length(client):
    assert client.post("/api/projects", json={**NEW_PROJECT, "minutes": 999}).status_code == 422


def test_list_and_delete(client):
    pid = make_project(client)
    assert any(p["id"] == pid for p in client.get("/api/projects").json()["projects"])

    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_patch_recomputes_the_budget_and_writes_the_export(client):
    pid = make_project(client)
    doc = client.patch(
        f"/api/projects/{pid}", json={"minutes": 2.0, "script": "one two three"}
    ).json()

    assert doc["context"]["target_words"] == 300
    assert doc["script_meta"]["word_count"] == 3
    assert doc["script_meta"]["edited"] is True
    assert client.get(f"/api/projects/{pid}/export/script.txt").status_code == 200


# --- The wizard -------------------------------------------------------------


def test_titles_step(client, mimo):
    pid = make_project(client)
    titles = client.post(f"/api/projects/{pid}/titles").json()["titles"]

    assert [t["title"] for t in titles] == ["Title 0", "Title 1", "Title 2"]
    assert client.get(f"/api/projects/{pid}").json()["step"] == 2


def test_script_step_requires_a_title_or_topic(client, mimo):
    pid = make_project(client)
    result = client.post(f"/api/projects/{pid}/script", json={"notes": ""}).json()

    assert result["word_count"] == 140
    assert result["target_words"] == 150
    doc = client.get(f"/api/projects/{pid}").json()
    assert doc["step"] == 4
    assert doc["scenes"] == []


def test_regenerating_the_script_clears_stale_scenes(client, mimo):
    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")
    assert client.get(f"/api/projects/{pid}").json()["scenes"]

    client.post(f"/api/projects/{pid}/script", json={})
    doc = client.get(f"/api/projects/{pid}").json()

    assert doc["scenes"] == []
    assert doc["thumbnail_prompt"] == ""
    assert doc["voiceover"]["status"] == "idle"


def test_scenes_step_covers_the_whole_script(client, mimo):
    pid = make_project(client)
    script = client.post(f"/api/projects/{pid}/script", json={}).json()["script"]
    result = client.post(f"/api/projects/{pid}/scenes").json()

    scenes = result["scenes"]
    assert scenes
    assert result["failed"] == []
    assert result["thumbnail_prompt"] == "one bold face, extreme contrast"
    assert result["negative_prompt"]

    # An image is held for exactly its own narration, and the starts accumulate
    # — the audio is one continuous track, so anything else would drift.
    assert all(s["target_sec"] == 3 for s in scenes)
    assert [s["index"] for s in scenes] == list(range(1, len(scenes) + 1))

    clock = 0.0
    for s in scenes:
        assert s["start_sec"] == pytest.approx(clock, abs=0.05)
        assert s["duration_sec"] == s["est_sec"]
        clock += s["duration_sec"]

    wpm = NEW_PROJECT["wpm"]
    assert clock == pytest.approx(len(script.split()) / wpm * 60, rel=0.02)

    # No word of the script is lost in the breakdown.
    assert " ".join(s["narration"] for s in scenes).split() == script.split()
    assert all(s["image_prompt"] for s in scenes)
    assert all("video_prompt" not in s for s in scenes)
    # Shots cycle so a long montage is not 200 identical wide shots.
    assert len({s["shot"] for s in scenes}) > 1


def test_a_shorter_cadence_produces_more_scenes(client, mimo):
    slow = make_project(client)
    client.post(f"/api/projects/{slow}/script", json={})
    slow_count = len(client.post(f"/api/projects/{slow}/scenes").json()["scenes"])

    fast = client.post("/api/projects", json={**NEW_PROJECT, "scene_seconds": 2}).json()["id"]
    client.post(f"/api/projects/{fast}/script", json={})
    fast_result = client.post(f"/api/projects/{fast}/scenes").json()

    assert len(fast_result["scenes"]) > slow_count
    assert all(s["target_sec"] == 2 for s in fast_result["scenes"])
    # Cutting faster means each image is up for less time.
    assert max(s["duration_sec"] for s in fast_result["scenes"]) <= 3


def test_scenes_require_a_script(client, mimo):
    pid = make_project(client)
    assert client.post(f"/api/projects/{pid}/scenes").status_code == 400


def test_failed_batches_leave_retryable_scenes(client, mimo, monkeypatch):
    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})

    def only_the_bible(system, user, **kwargs):
        if "Define the visual system" in user:
            return {"art_direction": "grainy"}
        if "thumbnail_prompt" in user:
            return {"thumbnail_prompt": "t"}
        raise RuntimeError("batch failed")

    monkeypatch.setattr(scenes_service, "chat_json", only_the_bible)
    result = client.post(f"/api/projects/{pid}/scenes").json()

    # Partial results are kept, never discarded.
    assert result["scenes"]
    assert result["failed"] == [s["index"] for s in result["scenes"]]
    assert all(s["narration"] for s in result["scenes"])


def test_retry_repairs_the_failed_scenes(client, mimo, monkeypatch):
    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})

    working = scenes_service.chat_json

    def bible_only(system, user, **kwargs):
        if "Define the visual system" in user or "thumbnail_prompt" in user:
            return working(system, user, **kwargs)
        raise RuntimeError("batch failed")

    monkeypatch.setattr(scenes_service, "chat_json", bible_only)
    client.post(f"/api/projects/{pid}/scenes")

    monkeypatch.setattr(scenes_service, "chat_json", working)
    retried = client.post(f"/api/projects/{pid}/scenes/retry").json()

    assert retried["failed"] == []
    assert all(s["image_prompt"] for s in retried["scenes"])


# --- Exports ----------------------------------------------------------------


def test_csv_export_shape(client, mimo):
    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")

    response = client.get(f"/api/projects/{pid}/export/prompts.csv")
    assert response.status_code == 200

    rows = list(csv.reader(io.StringIO(response.text.lstrip("﻿"))))
    assert rows[0] == [
        "scene",
        "start",
        "end",
        "duration_sec",
        "target_sec",
        "shot",
        "focus",
        "narration",
        "image_prompt",
        "negative_prompt",
    ]
    assert rows[1][0] == "1"
    assert rows[-1][0] == "thumbnail"
    # Every row carries the negative prompt, so one paste covers the batch.
    assert all(row[-1] for row in rows[1:])


def test_exports_404_before_they_exist(client):
    pid = make_project(client)
    assert client.get(f"/api/projects/{pid}/export/script.txt").status_code == 404
    assert client.get(f"/api/projects/{pid}/export/prompts.csv").status_code == 404


def test_bundle_zip_contains_the_artefacts(client, mimo):
    import zipfile

    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")

    response = client.get(f"/api/projects/{pid}/export/bundle.zip")
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()

    assert "project.json" in names
    assert "script.txt" in names
    assert "prompts.csv" in names


# --- Voices -----------------------------------------------------------------


def test_voice_crud(client):
    vid = make_voice(client)

    voice = client.get(f"/api/voices/{vid}").json()
    assert voice["name"] == "Narrator"
    assert voice["stability"] == 70
    assert "Steady, consistent, even tone." in voice["style_prompt"]
    assert voice["sample_warning"] is False

    updated = client.patch(f"/api/voices/{vid}", json={"name": "Renamed", "style": 90}).json()
    assert updated["name"] == "Renamed"
    assert "Strongly stylized" in updated["style_prompt"]

    assert client.get(f"/api/voices/{vid}/sample").status_code == 200
    assert client.delete(f"/api/voices/{vid}").status_code == 204
    assert client.get(f"/api/voices/{vid}").status_code == 404


def test_voice_rejects_a_non_audio_sample(client):
    response = client.post(
        "/api/voices",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"name": "Bad"},
    )
    assert response.status_code == 400


def test_voice_rejects_an_empty_name(client):
    response = client.post(
        "/api/voices",
        files={"file": ("s.wav", make_wav(0.2), "audio/wav")},
        data={"name": "   "},
    )
    assert response.status_code == 400


def test_style_presets_are_listed(client):
    presets = client.get("/api/style-presets").json()["presets"]
    assert presets
    assert {"id", "name", "prompt"} <= set(presets[0])


def test_preview_returns_audio(client, mimo):
    vid = make_voice(client)
    response = client.post(f"/api/voices/{vid}/preview", json={"text": "Testing one two."})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"


# --- Voice-over -------------------------------------------------------------


def test_voiceover_produces_one_full_track(client, mimo):
    from app.storage import audio_dir, project_dir

    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")
    vid = make_voice(client)

    started = client.post(f"/api/projects/{pid}/voiceover", json={"voice_id": vid})
    assert started.status_code == 202

    state = client.get(f"/api/projects/{pid}/voiceover/status").json()
    assert state["status"] == "done"
    assert state["failed"] == []
    assert state["done"] == state["total"]
    assert state["full_file"] == "voiceover_full.wav"
    assert state["full_sec"] > 0

    audio = client.get(f"/api/projects/{pid}/audio/voiceover_full.wav")
    assert audio.status_code == 200
    assert audio.content[:4] == b"RIFF"

    # One track, not one file per scene.
    rendered = {p.name for p in audio_dir(pid).iterdir() if p.is_file()}
    assert rendered == {"voiceover_full.wav"}

    # The pace the model actually spoke at, so the wpm setting can be corrected.
    # The stub renders each chunk in half a second, an absurd pace, so this also
    # exercises the clamp to the range the API will accept back.
    assert state["measured_wpm"] == 220

    # The CSV on disk is rewritten with the measured speaking times.
    csv_on_disk = (project_dir(pid) / "prompts.csv").read_text(encoding="utf-8-sig")
    assert csv_on_disk.splitlines()[1].split(",")[4] not in ("", "0.0")


def test_the_chunk_pieces_stay_out_of_the_bundle(client, mimo):
    import zipfile

    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")
    vid = make_voice(client)
    client.post(f"/api/projects/{pid}/voiceover", json={"voice_id": vid})

    names = zipfile.ZipFile(
        io.BytesIO(client.get(f"/api/projects/{pid}/export/bundle.zip").content)
    ).namelist()

    assert "audio/voiceover_full.wav" in names
    assert not any(n.startswith("audio/chunks/") for n in names)


def test_voiceover_requires_a_script(client, mimo):
    pid = make_project(client)
    vid = make_voice(client)
    assert (
        client.post(f"/api/projects/{pid}/voiceover", json={"voice_id": vid}).status_code == 400
    )


def test_a_failing_chunk_is_recorded_not_fatal(client, mimo, monkeypatch):
    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")
    vid = make_voice(client)

    monkeypatch.setattr(
        voiceover_service,
        "synth_clone",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("upstream hiccup")),
    )
    monkeypatch.setattr(voiceover_service, "VOICEOVER_RETRIES", 0)
    client.post(f"/api/projects/{pid}/voiceover", json={"voice_id": vid})

    state = client.get(f"/api/projects/{pid}/voiceover/status").json()
    # A hole in the middle of a sentence is named, not hidden.
    assert state["status"] == "partial"
    assert state["failed"]
    assert state["errors"]


def test_retry_re_renders_only_the_missing_pieces(client, mimo, monkeypatch):
    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")
    vid = make_voice(client)

    monkeypatch.setattr(
        voiceover_service,
        "synth_clone",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    monkeypatch.setattr(voiceover_service, "VOICEOVER_RETRIES", 0)
    client.post(f"/api/projects/{pid}/voiceover", json={"voice_id": vid})
    assert client.get(f"/api/projects/{pid}/voiceover/status").json()["status"] == "partial"

    monkeypatch.setattr(voiceover_service, "synth_clone", lambda *a, **k: make_wav(0.5))
    assert client.post(f"/api/projects/{pid}/voiceover/retry").status_code == 202

    state = client.get(f"/api/projects/{pid}/voiceover/status").json()
    assert state["status"] == "done"
    assert state["failed"] == []
    assert state["full_file"] == "voiceover_full.wav"


def test_retry_is_refused_once_the_track_is_complete(client, mimo):
    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")
    vid = make_voice(client)
    client.post(f"/api/projects/{pid}/voiceover", json={"voice_id": vid})

    assert client.post(f"/api/projects/{pid}/voiceover/retry").status_code == 400


def test_editing_the_script_keeps_the_scenes_but_flags_them(client, mimo):
    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")

    edited = client.patch(
        f"/api/projects/{pid}", json={"script": "A completely different script."}
    ).json()

    # An edit is not a regeneration: dozens of checked prompts are not thrown
    # away over a typo fix, but they are marked as no longer matching.
    assert edited["scenes"]
    assert edited["scenes_stale"] is True

    # Regenerating the scenes clears the flag.
    assert client.post(f"/api/projects/{pid}/scenes").status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["scenes_stale"] is False


def test_an_unchanged_script_patch_does_not_flag_the_scenes(client, mimo):
    pid = make_project(client)
    script = client.post(f"/api/projects/{pid}/script", json={}).json()["script"]
    client.post(f"/api/projects/{pid}/scenes")

    client.patch(f"/api/projects/{pid}", json={"script": script})
    assert client.get(f"/api/projects/{pid}").json().get("scenes_stale") is not True


def test_a_job_that_died_with_the_server_is_reported_as_interrupted(client, mimo):
    from app.storage import load_project, save_project

    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")

    # A crash mid-job leaves "running" on disk with no job in this process.
    doc = load_project(pid)
    doc["voiceover"] = {"status": "running", "voice_id": "gone", "total": 8, "done": 3}
    save_project(doc)

    state = client.get(f"/api/projects/{pid}/voiceover/status").json()
    assert state["status"] == "interrupted"
    assert state["running"] is False
    assert "restarted" in state["error"]
    # And the correction is persisted, not recomputed on every read.
    assert load_project(pid)["voiceover"]["status"] == "interrupted"


def test_retry_after_an_interruption_resumes(client, mimo):
    from app.storage import load_project, save_project

    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")
    vid = make_voice(client)

    doc = load_project(pid)
    doc["voiceover"] = {"status": "interrupted", "voice_id": vid, "failed": []}
    save_project(doc)

    assert client.post(f"/api/projects/{pid}/voiceover/retry").status_code == 202
    assert client.get(f"/api/projects/{pid}/voiceover/status").json()["status"] == "done"


def test_a_concurrent_edit_survives_the_voiceover_job(client, mimo):
    """The job runs for minutes; edits made in that window must not be erased."""
    from app.services.voiceover import _save
    from app.storage import load_project

    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")

    client.patch(f"/api/projects/{pid}", json={"selected_title": "Retitled mid-job"})
    _save(pid, {"status": "done", "done": 1}, 120)

    doc = load_project(pid)
    assert doc["selected_title"] == "Retitled mid-job"
    assert doc["voiceover"]["status"] == "done"
    # The measured pace is stamped onto the scenes that are on disk now.
    assert doc["scenes"][0]["actual_sec"] > 0


def test_a_script_too_long_for_the_scene_cap_reports_what_was_dropped(
    client, mimo, monkeypatch
):
    from app.services import scenes as scenes_module

    monkeypatch.setattr(scenes_module, "MAX_SCENES", 3)
    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    result = client.post(f"/api/projects/{pid}/scenes").json()

    assert len(result["scenes"]) == 3
    # Silence here would hand over a package that does not cover the script.
    assert result["dropped_scenes"] > 0


def test_regenerating_the_script_removes_the_stale_prompts_file(client, mimo):
    from app.storage import project_dir

    pid = make_project(client)
    client.post(f"/api/projects/{pid}/script", json={})
    client.post(f"/api/projects/{pid}/scenes")
    assert (project_dir(pid) / "prompts.csv").exists()

    client.post(f"/api/projects/{pid}/script", json={})

    # The scenes were cleared, so a CSV describing them must not survive — in
    # the directory or in the bundle download.
    assert not (project_dir(pid) / "prompts.csv").exists()


def test_a_non_object_json_response_is_a_clean_502(client, monkeypatch):
    import app.mimo as mimo_module

    class FakeMessage:
        content = '["not", "an", "object"]'

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return FakeCompletion()

    monkeypatch.setattr(mimo_module, "get_client", lambda: FakeClient())
    pid = make_project(client)

    # A list would be an AttributeError deep in a service without the guard.
    assert client.post(f"/api/projects/{pid}/titles").status_code == 502


def test_audio_path_traversal_is_rejected(client):
    pid = make_project(client)
    assert client.get(f"/api/projects/{pid}/audio/..%2F..%2Fproject.json").status_code == 404
    assert client.get(f"/api/projects/{pid}/audio/%2e%2e").status_code == 404


def test_deleting_a_dot_dot_project_cannot_wipe_the_data_directory(client):
    """A single unauthenticated DELETE must not be able to empty the store."""
    from app.config import PROJECTS_DIR, VOICES_PATH

    pid = make_project(client)
    make_voice(client)

    for attempt in ("%2e%2e", "%2E%2E", "..", ".", "%2e"):
        response = client.request("DELETE", f"/api/projects/{attempt}")
        assert response.status_code in (307, 400, 404, 405), attempt

    # Everything is still there.
    assert (PROJECTS_DIR / pid).is_dir()
    assert VOICES_PATH.exists()
    assert client.get("/api/voices").json()["voices"]
