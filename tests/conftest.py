"""Test setup.

The data directory is redirected before `app` is imported anywhere, so the
suite never touches the real `data/` tree.
"""

import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="ytc-tests-")
os.environ["YTC_DATA_DIR"] = _TMP
os.environ.setdefault("MIMO_API_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_data():
    """Start every test from an empty store."""
    from app.config import NICHES_DIR, PROJECTS_DIR, SAMPLES_DIR, VOICES_PATH, ensure_dirs

    shutil.rmtree(PROJECTS_DIR, ignore_errors=True)
    shutil.rmtree(SAMPLES_DIR, ignore_errors=True)
    shutil.rmtree(NICHES_DIR, ignore_errors=True)
    if VOICES_PATH.exists():
        VOICES_PATH.unlink()
    ensure_dirs()
    yield


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_wav(seconds: float = 1.0, sample_rate: int = 24000) -> bytes:
    """A silent but structurally valid WAV."""
    from app.audio import build_wav

    frames = int(sample_rate * seconds)
    return build_wav(b"\x00\x00" * frames, sample_rate, 1, 16)
