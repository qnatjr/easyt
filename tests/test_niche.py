from app.services import niche as niche_service
from app.services.niche import (
    EMPTY_PROFILE,
    join_transcript,
    normalise_profile,
    plan_clips,
)


def test_a_video_shorter_than_one_clip_is_used_whole():
    assert plan_clips(19.0) == [(0.0, 19.0)]


def test_no_planned_clip_ever_exceeds_the_cap():
    # The gateway accepts 50s and rejects 55s, so 45 is the cap we enforce
    # ourselves rather than letting it surface as "Multimodal data is corrupted".
    for duration in (19, 44, 45, 60, 100, 180, 600, 3600):
        assert all(length <= 45.0 for _, length in plan_clips(float(duration))), duration


def test_a_one_minute_video_yields_one_clip_not_four_overlapping_ones():
    # Four clips at 0/15/30/45s would each be 45s long and overlap almost
    # entirely: four times the token cost for nearly the same footage.
    assert plan_clips(60.0) == [(0.0, 45.0)]


def test_a_three_minute_video_yields_four_contiguous_clips():
    assert plan_clips(180.0) == [(0.0, 45.0), (45.0, 45.0), (90.0, 45.0), (135.0, 45.0)]


def test_a_ten_minute_video_yields_four_clips_spread_across_it():
    assert plan_clips(600.0) == [(0.0, 45.0), (150.0, 45.0), (300.0, 45.0), (450.0, 45.0)]


def test_no_clip_runs_past_the_end_of_the_video():
    for duration in (19, 45, 46, 89, 90, 134, 180, 599, 600, 4000):
        for start, length in plan_clips(float(duration)):
            assert start + length <= duration + 1e-9, (duration, start, length)


def test_a_video_with_no_duration_yields_nothing():
    assert plan_clips(0.0) == []
    assert plan_clips(-5.0) == []


# --- Profile shape ----------------------------------------------------------


def test_the_profile_always_has_every_field():
    profile = normalise_profile({})
    assert set(profile) == set(EMPTY_PROFILE)


def test_junk_from_the_model_becomes_an_empty_profile():
    assert normalise_profile({"nothing": "useful"}) == EMPTY_PROFILE
    assert normalise_profile(None) == EMPTY_PROFILE
    assert normalise_profile("a bare string") == EMPTY_PROFILE


def test_a_string_where_a_list_belongs_is_wrapped():
    profile = normalise_profile({"hook_patterns": "opens on a question"})
    assert profile["hook_patterns"] == ["opens on a question"]


def test_a_list_where_a_string_belongs_is_joined():
    profile = normalise_profile({"tone": ["wry", "conversational"]})
    assert profile["tone"] == "wry conversational"


def test_unknown_fields_are_dropped():
    profile = normalise_profile({"tone": "wry", "invented_field": "nonsense"})
    assert "invented_field" not in profile
    assert profile["tone"] == "wry"


def test_a_non_answer_is_treated_as_no_answer():
    # Observed live: asked about title style from a video with no title on
    # screen, the model answered "Not specified in the material." Left alone
    # that sentence is injected into the titles prompt as instruction.
    for phrase in (
        "Not specified in the material.",
        "not specified",
        "N/A",
        "unknown",
        "Not applicable",
        "None",
        "not enough information to determine",
    ):
        assert normalise_profile({"title_style": phrase})["title_style"] == "", phrase


def test_a_real_answer_that_merely_mentions_none_is_kept():
    profile = normalise_profile({"tone": "Blunt, with none of the usual warmth"})
    assert profile["tone"] == "Blunt, with none of the usual warmth"


def test_non_answers_are_dropped_from_lists_too():
    profile = normalise_profile({"avoid": ["stock footage", "N/A", "not specified"]})
    assert profile["avoid"] == ["stock footage"]


def test_blank_list_entries_are_dropped():
    profile = normalise_profile({"vocabulary": ["real", "  ", "", None, "also real"]})
    assert profile["vocabulary"] == ["real", "also real"]


def test_a_profile_with_nothing_in_it_is_falsy_by_emptiness():
    # The UI and the prompt builders both branch on "is there anything here",
    # so an all-blank profile must not read as populated.
    assert not any(EMPTY_PROFILE.values())


# --- Transcript assembly ----------------------------------------------------


def test_transcript_slices_are_joined_in_clip_order():
    results = [
        {"start": 0.0, "transcript": "first part"},
        {"start": 45.0, "transcript": "second part"},
    ]
    assert join_transcript(results) == "first part\n\nsecond part"


def test_transcript_joining_skips_failed_clips():
    # A clip that failed twice is None. The rest of the reference is still
    # worth keeping rather than throwing the whole run away.
    results = [
        {"start": 0.0, "transcript": "kept"},
        None,
        {"start": 90.0, "transcript": "also kept"},
    ]
    assert join_transcript(results) == "kept\n\nalso kept"


def test_transcript_joining_sorts_by_start_not_arrival():
    # Clips run concurrently, so they finish out of order.
    results = [
        {"start": 90.0, "transcript": "third"},
        {"start": 0.0, "transcript": "first"},
        {"start": 45.0, "transcript": "second"},
    ]
    assert join_transcript(results) == "first\n\nsecond\n\nthird"


def test_no_usable_clips_gives_an_empty_transcript():
    assert join_transcript([None, None]) == ""
    assert join_transcript([]) == ""


# --- Merging ----------------------------------------------------------------


def test_merging_asks_the_model_and_normalises_what_comes_back(monkeypatch):
    seen = {}

    def fake_chat_json(system, user, **kwargs):
        seen["user"] = user
        return {"tone": ["wry"], "invented": "x", "visual_style": "cold blues"}

    monkeypatch.setattr(niche_service, "chat_json", fake_chat_json)

    profile = niche_service.merge_partials(
        [{"start": 0.0, "transcript": "t", "tone": "wry"}], None, ""
    )

    assert profile["tone"] == "wry"
    assert profile["visual_style"] == "cold blues"
    assert "invented" not in profile
    assert set(profile) == set(EMPTY_PROFILE)


def test_merging_with_nothing_to_merge_does_not_call_the_model(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("the model must not be called with no material")

    monkeypatch.setattr(niche_service, "chat_json", explode)

    assert niche_service.merge_partials([], None, "") == EMPTY_PROFILE


def test_reference_block_renders_only_the_fields_it_is_given():
    profile = {**EMPTY_PROFILE, "tone": "wry", "visual_style": "cold blues"}

    block = niche_service.reference_block(profile, ("tone",))

    assert "wry" in block
    assert "cold blues" not in block


def test_reference_block_is_empty_when_the_profile_has_nothing():
    assert niche_service.reference_block(EMPTY_PROFILE, ("tone", "pacing")) == ""
    assert niche_service.reference_block({}, ("tone",)) == ""
    assert niche_service.reference_block(None, ("tone",)) == ""


def test_reference_block_skips_blank_fields_rather_than_labelling_them():
    profile = {**EMPTY_PROFILE, "tone": "wry", "pacing": ""}

    block = niche_service.reference_block(profile, ("tone", "pacing"))

    assert "wry" in block
    assert "pacing" not in block.lower()


def test_merging_still_runs_on_a_pasted_transcript_alone(monkeypatch):
    # No video, no screenshots — just text the creator pasted. That is a
    # complete and legitimate niche, and it must still produce a profile.
    monkeypatch.setattr(niche_service, "chat_json", lambda *a, **k: {"tone": "brisk"})

    profile = niche_service.merge_partials([], None, "a pasted transcript")
    assert profile["tone"] == "brisk"
