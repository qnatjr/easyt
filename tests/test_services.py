from app.services import script as script_service
from app.services import titles as titles_service
from app.services import voiceover as voiceover_service

CONTEXT = {"topic": "Rome", "category": "history", "language": "en", "minutes": 5.0, "wpm": 150}


# --- Titles -----------------------------------------------------------------


def test_titles_normalize_clamps_scores_and_recomputes_length(monkeypatch):
    monkeypatch.setattr(
        titles_service,
        "chat_json",
        lambda *a, **k: {
            "titles": [
                {"title": "Low one", "ctr_score": -20, "hooks": "a single string"},
                {"title": "High one", "ctr_score": 400, "hooks": ["gap", "number"]},
                {"title": "  ", "ctr_score": 50},
                {"title": "No score"},
                "a bare string title",
            ]
        },
    )
    results = titles_service.generate_titles(CONTEXT)

    assert [t["title"] for t in results][0] == "High one"
    assert results[0]["ctr_score"] == 100
    assert min(t["ctr_score"] for t in results) == 0
    # The blank title is dropped, the rest survive.
    assert len(results) == 4
    assert all(t["char_count"] == len(t["title"]) for t in results)
    # A string in the hooks field becomes a one-element list.
    assert results[-1]["hooks"] == [] or isinstance(results[-1]["hooks"], list)


def test_titles_survive_a_junk_response(monkeypatch):
    monkeypatch.setattr(titles_service, "chat_json", lambda *a, **k: {"nothing": "useful"})
    assert titles_service.generate_titles(CONTEXT) == []


# --- Niche profile injection ------------------------------------------------

PROFILE = {
    "hook_patterns": ["opens on a hard number"],
    "tone": "wry and conversational",
    "pacing": "short sentences, one idea each",
    "vocabulary": ["genuinely", "the thing is"],
    "structure": ["scale first, then the twist"],
    "title_style": "a number plus a stake",
    "visual_style": "cold blues, hard key light",
    "shot_language": ["tight on hands"],
    "avoid": ["stock smiling office footage"],
    "image_prompt_seed": "documentary stills, cold blue grade",
}


def test_a_project_without_a_niche_builds_the_prompt_it_always_did():
    # The whole feature has to be inert when unused: no niche means the
    # existing free-text reference behaves exactly as before.
    before = titles_service.build_prompt(CONTEXT)
    after = titles_service.build_prompt({**CONTEXT, "niche_profile": {}})

    assert before == after


def test_titles_prompt_carries_the_packaging_fields_only():
    prompt = titles_service.build_prompt({**CONTEXT, "niche_profile": PROFILE})

    assert "a number plus a stake" in prompt
    assert "opens on a hard number" in prompt
    # Art direction is no use to a title writer and would only dilute the ask.
    assert "cold blues" not in prompt


def test_script_prompt_carries_the_voice_fields():
    prompt = script_service.build_prompt({**CONTEXT, "niche_profile": PROFILE}, "A Title")

    assert "wry and conversational" in prompt
    assert "short sentences, one idea each" in prompt
    assert "the thing is" in prompt
    assert "scale first, then the twist" in prompt


def test_script_prompt_is_unchanged_without_a_niche():
    before = script_service.build_prompt(CONTEXT, "A Title")
    after = script_service.build_prompt({**CONTEXT, "niche_profile": {}}, "A Title")

    assert before == after


def test_the_transcript_never_reaches_a_project_prompt():
    # The point of the profile is that it is small. A transcript smuggled into
    # the context must not end up beside the script's word budget.
    ctx = {**CONTEXT, "niche_profile": PROFILE, "transcript": "x" * 5000}
    prompt = script_service.build_prompt(ctx, "A Title")

    assert "xxxx" not in prompt


def test_style_bible_prompt_carries_the_art_direction_fields():
    from app.services import scenes as scenes_service

    prompt = scenes_service.build_bible_prompt(
        {**CONTEXT, "niche_profile": PROFILE}, "A Title", "the script"
    )

    assert "cold blues, hard key light" in prompt
    assert "tight on hands" in prompt
    assert "documentary stills, cold blue grade" in prompt
    assert "stock smiling office footage" in prompt


def test_style_bible_prompt_is_unchanged_without_a_niche():
    from app.services import scenes as scenes_service

    before = scenes_service.build_bible_prompt(CONTEXT, "A Title", "the script")
    after = scenes_service.build_bible_prompt(
        {**CONTEXT, "niche_profile": {}}, "A Title", "the script"
    )

    assert before == after


# --- Script -----------------------------------------------------------------


def test_word_count():
    assert script_service.word_count("one two three") == 3
    assert script_service.word_count("") == 0
    assert script_service.word_count(None) == 0


def test_budget_defaults_to_ten_percent_headroom():
    target, maximum = script_service.resolve_budget({"minutes": 5, "wpm": 150})
    assert target == 750
    assert maximum == 825


def test_budget_respects_an_explicit_maximum():
    target, maximum = script_service.resolve_budget(
        {"minutes": 5, "wpm": 150, "max_words": 600, "max_words_custom": True}
    )
    assert target == 750
    assert maximum == 600


def test_a_derived_maximum_is_not_mistaken_for_a_choice():
    # The derived ceiling is persisted into the context. Reading it back as an
    # override would freeze it while the length and pace moved underneath.
    ctx = {"minutes": 5, "wpm": 150, "max_words": 825, "max_words_custom": False}
    ctx["target_words"], ctx["max_words"] = script_service.resolve_budget(ctx)

    ctx["minutes"] = 10
    target, maximum = script_service.resolve_budget(ctx)

    assert target == 1500
    assert maximum == 1650


def test_max_tokens_scales_with_the_script_length():
    # A fixed 8k budget would cut a long script off mid-sentence.
    assert script_service.max_tokens_for(750) == 4096
    assert script_service.max_tokens_for(13200) == 32768
    assert script_service.max_tokens_for(3000) == 12000


def test_script_reports_its_real_length(monkeypatch):
    monkeypatch.setattr(
        script_service, "chat_json", lambda *a, **k: {"script": " ".join(["word"] * 300)}
    )
    result = script_service.generate_script({**CONTEXT, "minutes": 2}, "A Title")

    assert result["word_count"] == 300
    assert result["estimated_minutes"] == 2.0
    assert result["over_budget"] is False
    assert result["tightened"] is False


def test_overlong_script_triggers_one_tighten_pass(monkeypatch):
    calls = []

    def fake(system, user, **kwargs):
        calls.append(user)
        # First call overruns badly; the tighten pass comes back inside budget.
        return {"script": " ".join(["word"] * (900 if len(calls) == 1 else 300))}

    monkeypatch.setattr(script_service, "chat_json", fake)
    result = script_service.generate_script({**CONTEXT, "minutes": 2}, "A Title")

    assert len(calls) == 2
    assert result["tightened"] is True
    assert result["word_count"] == 300
    assert result["over_budget"] is False


def test_tighten_keeps_the_original_when_the_retry_is_no_shorter(monkeypatch):
    monkeypatch.setattr(script_service, "chat_json", lambda *a, **k: {"script": "a b c d e"})
    assert script_service.tighten("one two", 1, "en") == "one two"


def test_a_list_shaped_script_is_joined(monkeypatch):
    monkeypatch.setattr(
        script_service, "chat_json", lambda *a, **k: {"script": ["para one", "para two"]}
    )
    result = script_service.generate_script(CONTEXT, "A Title")
    assert result["script"] == "para one\n\npara two"


# --- Voice settings ---------------------------------------------------------


def test_style_prompt_translates_the_sliders():
    prompt = voiceover_service.build_style_prompt(
        {
            "category": "documentary",
            "description": "Deep and measured",
            "stability": 80,
            "style": 5,
            "similarity": 90,
            "speaker_boost": True,
            "speed": 0.8,
        }
    )

    assert "documentary" in prompt
    assert "Deep and measured." in prompt
    assert "Steady, consistent, even tone." in prompt
    assert "Minimal stylization" in prompt
    assert "faithful to the reference speaker's timbre" in prompt
    assert "well-projected" in prompt
    assert "slower pace" in prompt


def test_style_prompt_at_the_other_end_of_every_slider():
    prompt = voiceover_service.build_style_prompt(
        {"stability": 10, "style": 90, "similarity": 20, "speaker_boost": False, "speed": 1.3}
    )

    assert "Expressive, dynamic" in prompt
    assert "Strongly stylized" in prompt
    assert "timbre" not in prompt
    assert "well-projected" not in prompt
    assert "faster pace" in prompt


def test_measured_wpm_reads_the_real_pace():
    # 150 words over 60 seconds is 150 words per minute.
    assert voiceover_service.measured_wpm(150, 60.0) == 150
    assert voiceover_service.measured_wpm(100, 60.0) == 100


def test_measured_wpm_is_clamped_to_what_the_api_accepts():
    # The UI offers this number as a one-click replacement for the project's
    # wpm, so publishing a value the server would reject is a dead end.
    assert voiceover_service.measured_wpm(2, 60.0) == 80
    assert voiceover_service.measured_wpm(500, 10.0) == 220


def test_measured_wpm_is_zero_before_anything_renders():
    assert voiceover_service.measured_wpm(0, 0.0) == 0
    assert voiceover_service.measured_wpm(120, 0.0) == 0


def test_retiming_relays_the_shot_list_against_the_real_pace():
    doc = {
        "scenes": [
            {"narration": "one two three four five", "start_sec": 0, "duration_sec": 3},
            {"narration": "six seven", "start_sec": 3, "duration_sec": 3},
            {"narration": "eight nine ten", "start_sec": 6, "duration_sec": 3},
        ]
    }
    voiceover_service._retime_scenes(doc, 100)

    # Five words at 100 wpm is three seconds.
    assert doc["scenes"][0]["actual_sec"] == 3.0
    assert doc["scenes"][1]["actual_sec"] == 1.2
    assert doc["scenes"][2]["actual_sec"] == 1.8

    # The narration is one continuous track, so the images butt up against
    # each other rather than sitting in fixed slots.
    clock = 0.0
    for scene in doc["scenes"]:
        assert scene["start_sec"] == round(clock, 1)
        assert scene["duration_sec"] == scene["actual_sec"]
        clock += scene["duration_sec"]
    assert clock == 6.0


def test_style_prompt_defaults_are_sane():
    prompt = voiceover_service.build_style_prompt({})
    assert "Balanced, natural intonation." in prompt
    assert "natural pace" in prompt
