from app.audio import (
    build_wav,
    concat_wavs,
    parse_wav_header,
    split_sentences,
    split_text,
    timecode,
    wav_duration_seconds,
)

from tests.conftest import make_wav


def test_build_and_parse_round_trip():
    pcm = b"\x01\x02" * 100
    wav = build_wav(pcm, 24000, 1, 16)

    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert len(wav) == 44 + len(pcm)

    fmt, parsed = parse_wav_header(wav)
    assert fmt == {"channels": 1, "sample_rate": 24000, "bits": 16}
    assert parsed == pcm


def test_parse_rejects_non_wav():
    fmt, payload = parse_wav_header(b"not a wav at all")
    assert fmt is None
    assert payload == b"not a wav at all"


def test_concat_sums_payloads():
    a = build_wav(b"\x01\x02" * 50, 24000, 1, 16)
    b = build_wav(b"\x03\x04" * 70, 24000, 1, 16)

    joined = concat_wavs([a, b])
    fmt, pcm = parse_wav_header(joined)

    assert fmt["sample_rate"] == 24000
    assert len(pcm) == 100 + 140
    # The declared data length has to match the real payload.
    assert int.from_bytes(joined[40:44], "little") == len(pcm)


def test_concat_of_nothing_is_empty():
    assert concat_wavs([]) == b""
    assert concat_wavs([b"", b""]) == b""


def test_duration():
    assert wav_duration_seconds(make_wav(2.0)) == 2.0
    assert wav_duration_seconds(b"garbage") == 0.0


def test_split_sentences_keeps_punctuation():
    parts = split_sentences("One. Two! Three? Four")
    assert parts == ["One.", "Two!", "Three?", "Four"]


def test_split_text_under_limit_is_untouched():
    assert split_text("short text", 100) == ["short text"]
    assert split_text("   ", 100) == []


def test_split_text_breaks_on_sentences():
    text = " ".join(f"Sentence number {i} here." for i in range(60))
    chunks = split_text(text, 200)

    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    # Nothing may be lost in the split.
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_split_text_hard_splits_one_long_sentence():
    text = "word " * 200  # no terminal punctuation at all
    chunks = split_text(text, 100)

    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)


def test_timecode():
    # Tenths, because an image can be on screen for well under a second and a
    # shot list rounded to whole seconds would drift against the audio.
    assert timecode(0) == "00:00:00.0"
    assert timecode(2.8) == "00:00:02.8"
    assert timecode(75.4) == "00:01:15.4"
    assert timecode(3661) == "01:01:01.0"
    # Rounding must carry rather than produce ".10".
    assert timecode(1.97) == "00:00:02.0"
    assert timecode(-5) == "00:00:00.0"
