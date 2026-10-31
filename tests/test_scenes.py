from app.config import MAX_SCENES, SHOT_CYCLE
from app.services.scenes import (
    _normalize_batch,
    estimate_seconds,
    segment_beats,
    segment_script,
    shot_for,
    words_per_scene,
)


def batch_of(*indices) -> list[dict]:
    return [{"index": i, "text": f"beat {i}", "context": "", "shot": shot_for(i)} for i in indices]


# --- Word budget ------------------------------------------------------------


def test_words_per_scene_matches_the_pace():
    # 150 wpm over 3 seconds is 7 words; over 8 seconds it is 20.
    assert words_per_scene(150, 3) == 7
    assert words_per_scene(150, 8) == 20
    assert words_per_scene(120, 8) == 16
    # Never degenerate to zero, however short the scene.
    assert words_per_scene(80, 1) >= 2


def test_the_word_budget_is_floored_not_rounded():
    # 87/60*8 is 11.6. Rounding up to 12 words would take 8.28 seconds against
    # an 8-second slot, and the error compounds across hundreds of images.
    assert words_per_scene(87, 8) == 11
    assert words_per_scene(150, 3) == 7  # 7.5 floored


def test_a_full_beat_lands_on_the_target_cadence_at_any_supported_setting():
    # wpm is a free integer between 80 and 220, and the voice-over job feeds
    # measured values straight back into it, so every combination must hold.
    for seconds in range(2, 11):
        for wpm in range(80, 221):
            words = words_per_scene(wpm, seconds)
            narration = " ".join(["word"] * words)
            assert estimate_seconds(narration, wpm) <= seconds, (wpm, seconds)


# --- Segmentation -----------------------------------------------------------


def test_segmentation_covers_every_word_in_order():
    script = " ".join(f"word{i}" for i in range(203)) + "."
    segments = segment_script(script, 7)

    assert " ".join(segments).split() == script.split()


def test_beats_stay_close_to_the_target_cadence():
    """Beats may overshoot only to avoid a stub, and never by much.

    An image is held for exactly its own narration, so the cadence is a target
    rather than a slot. What matters is that the cut rhythm stays near what the
    creator asked for: no stubs, and nothing running away.
    """
    sentences = [f"This is sentence number {i} and it carries a little weight." for i in range(40)]

    for target in (4, 7, 14, 20, 33):
        segments = segment_script(" ".join(sentences), target)
        ceiling = max(target + 1, int(target * 1.5))
        floor_words = max(1, int(target * 0.4))
        assert segments
        assert all(len(s.split()) <= ceiling for s in segments), target
        assert all(len(s.split()) >= floor_words for s in segments), target


def test_a_stub_is_folded_into_its_neighbour():
    # "hari." on its own used to buy a whole image for one word.
    script = "Betapa kuno bangunan ini, tapi masih mengalirkan air setiap hari."
    beats = segment_beats(script, 5)

    assert all(len(b["text"].split()) >= 2 for b in beats)
    assert " ".join(b["text"] for b in beats).split() == script.split()


def test_a_stub_at_the_very_start_borrows_the_next_beat():
    script = "Ya. Kemudian semuanya berubah dalam waktu yang sangat singkat sekali."
    beats = segment_beats(script, 6)

    assert beats[0]["text"].split()[0] == "Ya."
    assert len(beats[0]["text"].split()) > 1


def test_the_overrun_case_that_slipped_through_before():
    # Three short sentences totalling 27 words used to land in one slot.
    script = (
        "Bangunan modern bisa hancur dalam puluhan tahun. "
        "Tapi aku air Romawi masih mengalir setelah dua milenium. "
        "Kenapa beton kita kalah dari teknologi dua ribu tahun lalu?"
    )
    segments = segment_script(script, 7)

    assert all(len(s.split()) <= 7 for s in segments)
    assert " ".join(segments).split() == script.split()


def test_long_sentence_is_broken_at_clauses():
    long_sentence = (
        "It began quietly, then it grew louder, and then it spread across the valley, "
        "until every village had heard it, and no one could pretend otherwise, "
        "which is how the story finally reached the capital."
    )
    segments = segment_script(long_sentence, 8)

    assert len(segments) > 1
    assert " ".join(segments).split() == long_sentence.split()
    assert all(len(s.split()) <= 12 for s in segments)
    on_clause = sum(1 for s in segments[:-1] if s.rstrip("\"'").endswith((",", ";", ":")))
    assert on_clause >= len(segments) // 2


def test_empty_script_yields_nothing():
    assert segment_script("", 7) == []
    assert segment_beats("   \n  ", 7) == []


def test_short_script_is_one_scene():
    assert segment_script("Only a handful of words.", 20) == ["Only a handful of words."]


def test_segments_do_not_collapse_into_stubs():
    sentences = [f"This is sentence number {i} and it carries weight." for i in range(30)]
    segments = segment_script(" ".join(sentences), 20)

    assert all(len(s.split()) >= 10 for s in segments[:-1])


def test_the_scene_cap_does_not_bite_a_legitimate_hour_long_project():
    sentence = "This is a sentence of a fairly typical length for spoken narration here. "
    script = sentence * 700  # 9100 words, about a 60-minute script at 150 wpm
    segments = segment_script(script, words_per_scene(150, 3))

    assert len(segments) > 1000, "three-second cutting makes a lot of images"
    assert len(segments) < MAX_SCENES, "the runaway guard must not bite real work"


# --- Beats carry their sentence --------------------------------------------


def test_a_beat_carries_the_sentence_it_came_from():
    """Seven words is often half a clause — useless alone as prompt input."""
    script = (
        "The aqueduct carried water across the valley for nine hundred years without a pump."
    )
    beats = segment_beats(script, 7)

    assert len(beats) > 1
    # Each beat is a fragment...
    assert all(len(b["text"].split()) <= 7 for b in beats)
    # ...but every one of them knows the whole line it belongs to.
    assert all(b["context"] == script for b in beats)


def test_context_spans_the_sentences_a_beat_merged():
    script = "One. Two. Three. Four."
    beats = segment_beats(script, 3)

    assert beats
    for beat in beats:
        for word in beat["text"].split():
            assert word in beat["context"]


# --- Shot rhythm ------------------------------------------------------------


def test_shots_cycle_so_the_montage_varies():
    shots = [shot_for(i) for i in range(1, len(SHOT_CYCLE) * 2 + 1)]

    assert shots[0] == SHOT_CYCLE[0]
    # A full cycle before anything repeats.
    assert len(set(shots[: len(SHOT_CYCLE)])) == len(SHOT_CYCLE)
    assert shots[len(SHOT_CYCLE)] == SHOT_CYCLE[0]


# --- Batch normalisation ----------------------------------------------------


def test_normalize_batch_matches_on_index():
    raw = {
        "scenes": [
            {"index": 2, "image_prompt": "i2", "focus": "f2"},
            {"index": 1, "image_prompt": "i1", "focus": "f1"},
        ]
    }
    result = _normalize_batch(raw, batch_of(1, 2))

    assert result[1]["image_prompt"] == "i1"
    assert result[2]["focus"] == "f2"


def test_normalize_batch_falls_back_to_position_when_the_model_renumbers():
    raw = {
        "scenes": [
            {"index": 1, "image_prompt": "i1"},
            {"index": 2, "image_prompt": "i2"},
        ]
    }
    result = _normalize_batch(raw, batch_of(11, 12))

    assert result[11]["image_prompt"] == "i1"
    assert result[12]["image_prompt"] == "i2"


def test_a_scene_without_an_image_prompt_is_not_accepted():
    # The image prompt is the deliverable. Accepting an entry without one would
    # mark the scene "ok" and leave a blank cell the retry path never revisits.
    raw = {"scenes": [{"index": 1, "image_prompt": "", "focus": "something"}]}

    assert _normalize_batch(raw, batch_of(1)) == {}


def test_normalize_batch_drops_malformed_entries():
    assert _normalize_batch({"scenes": ["junk", 5]}, batch_of(1, 2)) == {}
    assert _normalize_batch({}, batch_of(1)) == {}
    assert _normalize_batch({"scenes": "nope"}, batch_of(1)) == {}
