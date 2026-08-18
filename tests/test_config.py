import os

import pytest

from issuer_data.config import Settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Drop every setting inherited from the real environment.

    Without this the developer's own ISSUER_* / KRX_* values (or a CI secret)
    decide the assertions — and a failure would print the live key.
    """
    for name in list(os.environ):
        if name.startswith(("ISSUER_", "KRX_")):
            monkeypatch.delenv(name, raising=False)


def _settings() -> Settings:
    # _env_file=None so a developer's local .env can't leak into the assertions.
    return Settings(_env_file=None)


def test_krx_reads_unprefixed_names(monkeypatch):
    """pykrx reads KRX_ID / KRX_PW, so Settings must read the same names."""
    monkeypatch.setenv("KRX_ID", "member-id")
    monkeypatch.setenv("KRX_PW", "member-pw")
    s = _settings()
    assert s.krx_id == "member-id"
    assert s.krx_pw == "member-pw"


def test_krx_falls_back_to_issuer_prefix(monkeypatch):
    monkeypatch.setenv("ISSUER_KRX_ID", "member-id")
    monkeypatch.setenv("ISSUER_KRX_PW", "member-pw")
    s = _settings()
    assert s.krx_id == "member-id"
    assert s.krx_pw == "member-pw"


def test_krx_unprefixed_wins_over_prefixed(monkeypatch):
    monkeypatch.setenv("KRX_ID", "unprefixed")
    monkeypatch.setenv("ISSUER_KRX_ID", "prefixed")
    assert _settings().krx_id == "unprefixed"


def test_krx_absent_is_none():
    s = _settings()
    assert s.krx_id is None
    assert s.krx_pw is None


def test_other_keys_still_use_issuer_prefix(monkeypatch):
    """The alias is KRX-only; every other secret keeps the ISSUER_ prefix."""
    monkeypatch.setenv("ISSUER_DART_API_KEY", "dart-key")
    monkeypatch.setenv("FMP_API_KEY", "should-be-ignored")
    s = _settings()
    assert s.dart_api_key == "dart-key"
    assert s.fmp_api_key is None
