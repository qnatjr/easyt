"""WAV container helpers and text chunking.

The MIMO clone model returns a complete WAV per request. Joining several of
them means stripping each header, concatenating the PCM, and writing one fresh
header — which is what `concat_wavs` does.
"""

import re

from app.config import MAX_CHUNK_CHARS

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？…])[\s\n]+")


def parse_wav_header(raw: bytes) -> tuple[dict | None, bytes]:
    """Return (format dict, PCM payload). Format is None if `raw` is not a WAV."""
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None, raw
    channels = int.from_bytes(raw[22:24], "little")
    sample_rate = int.from_bytes(raw[24:28], "little")
    bits = int.from_bytes(raw[34:36], "little")
    data_idx = raw.find(b"data")
    if data_idx == -1:
        return None, raw
    pcm_start = data_idx + 8
    if not channels or not sample_rate or not bits:
        return None, raw
    return (
        {"channels": channels, "sample_rate": sample_rate, "bits": bits},
        raw[pcm_start:],
    )


def build_wav(pcm: bytes, sample_rate: int, channels: int, bits: int) -> bytes:
    """Wrap raw PCM in a canonical 44-byte RIFF/WAVE header."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_len = len(pcm)
    return (
        b"RIFF"
        + (36 + data_len).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + channels.to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + bits.to_bytes(2, "little")
        + b"data"
        + data_len.to_bytes(4, "little")
        + pcm
    )


def concat_wavs(wavs: list[bytes]) -> bytes:
    """Join WAVs that share a format into one. Returns b'' for an empty list."""
    pcm_all = bytearray()
    fmt: dict | None = None
    for wav in wavs:
        if not wav:
            continue
        parsed, pcm = parse_wav_header(wav)
        if parsed is None:
            # Unparseable container: append the payload as-is rather than drop it.
            pcm_all += pcm
            continue
        fmt = fmt or parsed
        pcm_all += pcm
    if not pcm_all:
        return b""
    if fmt is None:
        fmt = {"channels": 1, "sample_rate": 24000, "bits": 16}
    return build_wav(bytes(pcm_all), fmt["sample_rate"], fmt["channels"], fmt["bits"])


def wav_duration_seconds(wav: bytes) -> float:
    """Duration of a WAV in seconds, or 0.0 if the header cannot be read."""
    fmt, pcm = parse_wav_header(wav)
    if fmt is None:
        return 0.0
    bytes_per_second = fmt["sample_rate"] * fmt["channels"] * fmt["bits"] // 8
    if bytes_per_second <= 0:
        return 0.0
    return round(len(pcm) / bytes_per_second, 2)


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences, keeping terminal punctuation attached."""
    parts = _SENTENCE_SPLIT.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


def split_text(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Chunk text under `limit` characters, preferring sentence boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for sentence in split_sentences(text):
        if len(sentence) > limit:
            # A single sentence longer than the cap has to be hard-split.
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(sentence), limit):
                chunks.append(sentence[i : i + limit])
            continue
        if not cur:
            cur = sentence
        elif len(cur) + 1 + len(sentence) <= limit:
            cur = f"{cur} {sentence}"
        else:
            chunks.append(cur)
            cur = sentence
    if cur:
        chunks.append(cur)
    return chunks


def timecode(seconds: float) -> str:
    """Format seconds as HH:MM:SS.s.

    Tenths matter: an image can be on screen for well under a second, and a
    shot list rounded to whole seconds would drift against the audio.
    """
    total = max(0.0, float(seconds or 0))
    whole = int(total)
    tenths = int(round((total - whole) * 10))
    if tenths == 10:
        whole, tenths = whole + 1, 0
    return f"{whole // 3600:02d}:{(whole % 3600) // 60:02d}:{whole % 60:02d}.{tenths}"
