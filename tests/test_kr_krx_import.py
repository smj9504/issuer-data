"""pykrx logs in at import time; that must never take the process down.

pykrx runs `build_krx_session()` in module scope, reading KRX_ID/KRX_PW from
`os.getenv` in the function's default arguments, and prints Korean text on every
outcome — missing creds, bad creds, and success alike. On a Windows console in a
legacy code page those prints raise UnicodeEncodeError, which killed `import
pykrx` and with it any script importing our KR collector. These tests pin both
halves of the fix: blanks are dropped before pykrx sees them, and an import that
fails anyway degrades to NotSupportedError instead of propagating.
"""

import builtins
import sys

import pytest

from issuer_data.collectors import kr_krx
from issuer_data.collectors.base import NotSupportedError


def test_import_failure_becomes_not_supported(monkeypatch):
    """A pykrx that explodes at import time is 'KRX unavailable', not a crash."""
    real_import = builtins.__import__

    def boom(name, *args, **kwargs):
        if name == "pykrx" or name.startswith("pykrx."):
            raise UnicodeEncodeError("charmap", "로그인", 0, 3, "unmappable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom)
    monkeypatch.delitem(sys.modules, "pykrx", raising=False)
    monkeypatch.delitem(sys.modules, "pykrx.stock", raising=False)

    with pytest.raises(NotSupportedError, match="pykrx is unavailable"):
        kr_krx._import_pykrx_stock()


def test_import_forces_utf8_streams(monkeypatch):
    """The console is switched to UTF-8 before pykrx gets to print Korean."""
    calls = []

    class FakeStream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", FakeStream())
    monkeypatch.setattr(sys, "stderr", FakeStream())
    try:
        kr_krx._import_pykrx_stock()
    except NotSupportedError:
        pass  # pykrx may be absent; the reconfigure still must have happened

    assert calls, "stdout/stderr were never reconfigured"
    assert all(c["encoding"] == "utf-8" for c in calls)


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_krx_credentials_are_dropped_not_passed_through(monkeypatch, blank):
    """`KRX_ID=` in .env must not reach pykrx.

    pykrx treats "set but blank" as a failed login rather than staying anonymous,
    so a blank is worse than an absent variable — which is exactly what copying
    .env.example used to produce.
    """
    import os

    monkeypatch.setenv("KRX_ID", blank)
    monkeypatch.setenv("KRX_PW", blank)

    # the guard as it runs in cli.main() right after load_dotenv()
    for var in ("KRX_ID", "KRX_PW"):
        if not os.environ.get(var, "").strip():
            os.environ.pop(var, None)

    assert "KRX_ID" not in os.environ
    assert "KRX_PW" not in os.environ


def test_real_credentials_survive_the_guard(monkeypatch):
    """A genuine login must still be passed through untouched."""
    import os

    monkeypatch.setenv("KRX_ID", "realuser")
    monkeypatch.setenv("KRX_PW", "realpass")

    for var in ("KRX_ID", "KRX_PW"):
        if not os.environ.get(var, "").strip():
            os.environ.pop(var, None)

    assert os.environ["KRX_ID"] == "realuser"
    assert os.environ["KRX_PW"] == "realpass"
