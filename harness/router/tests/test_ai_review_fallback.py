"""ai_review.py model-fallback chain (2026-08-24).

Exercises the script end-to-end with urllib.request.urlopen stubbed, because the
logic worth pinning down is *which model gets called and what the comment claims*,
not the HTTP itself.

The first test is the important one: with AI_REVIEW_FALLBACK_MODELS unset the
script must behave exactly as it did before the chain existed.
"""
import io
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

import pytest

SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / ".github" / "scripts" / "ai_review.py"
E = urllib.error.HTTPError


class _FakeResp:
    def __init__(self, body): self._b = body
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _run(monkeypatch, tmp_path, env, responses):
    """Run ai_review.py against stubbed responses.

    responses maps model -> a review string, or an Exception to raise.
    Returns (models_called, review_markdown, failure_marker_written).
    """
    calls = []

    def fake_urlopen(req, timeout=None):
        model = json.loads(req.data.decode())["model"]
        calls.append(model)
        r = responses.get(model, E("u", 404, "Not Found", None, None))
        if isinstance(r, Exception):
            raise r
        return _FakeResp(json.dumps({"choices": [{"message": {"content": r}}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    # AI_REVIEW_DIR, so this writes inside pytest's tmp_path instead of a shared absolute
    # path on the host. It used to write /tmp/pr.diff, which on Windows is DRIVE-RELATIVE:
    # run from a repository on F: it created F:\tmp, and this whole file then depended on
    # that directory continuing to exist. Removing the stray directory failed ten tests
    # here, with FileNotFoundError on '\tmp\pr.diff'.
    #
    # tmp_path is per-test, so there is nothing to clean up between runs either -- the three
    # removals this replaced existed only because the directory was shared.
    monkeypatch.setenv("AI_REVIEW_DIR", str(tmp_path))
    (tmp_path / "pr.diff").write_text(
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    )

    buf, old_stdout = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        exec(compile(SCRIPT_PATH.read_text(), "ai_review.py", "exec"), {"__name__": "__main__"})
    except SystemExit:
        pass
    finally:
        sys.stdout = old_stdout

    review = tmp_path / "review.md"
    md = review.read_text() if review.exists() else ""
    return calls, md, (tmp_path / "ai_review_failed").exists()


BASE = {"OPENROUTER_API_KEY": "k"}


def test_unset_fallback_var_is_exactly_the_old_behaviour(monkeypatch, tmp_path):
    calls, md, failed = _run(monkeypatch, tmp_path,
                             {**BASE, "MODEL": "paid/x", "AI_REVIEW_FALLBACK_MODELS": ""},
                             {"paid/x": "LGTM"})
    assert calls == ["paid/x"]
    assert "model: paid/x" in md
    assert "FALLBACK MODEL" not in md
    assert not failed


def test_402_walks_to_the_free_model(monkeypatch, tmp_path):
    """The exact production failure: OpenRouter billing 402 on the paid model."""
    calls, md, failed = _run(monkeypatch, tmp_path,
                             {**BASE, "MODEL": "paid/x", "AI_REVIEW_FALLBACK_MODELS": "free/a, free/b"},
                             {"paid/x": E("u", 402, "Payment Required", None, None),
                              "free/a": "review from free"})
    assert calls == ["paid/x", "free/a"]
    assert not failed
    # The header must name the model that ACTUALLY ran, and disclose the downgrade.
    assert "model: free/a" in md
    assert "FALLBACK MODEL" in md
    assert "`paid/x` was unavailable" in md


def test_chain_continues_past_a_rate_limited_fallback(monkeypatch, tmp_path):
    calls, md, _ = _run(monkeypatch, tmp_path,
                        {**BASE, "MODEL": "paid/x", "AI_REVIEW_FALLBACK_MODELS": "free/a,free/b"},
                        {"paid/x": E("u", 402, "pay", None, None),
                         "free/a": E("u", 429, "rate", None, None),
                         "free/b": "third time"})
    assert calls == ["paid/x", "free/a", "free/b"]
    assert "model: free/b" in md


def test_401_is_not_retryable(monkeypatch, tmp_path):
    """A bad key fails identically for every model; walking the chain is pure noise."""
    calls, _, failed = _run(monkeypatch, tmp_path,
                            {**BASE, "MODEL": "paid/x", "AI_REVIEW_FALLBACK_MODELS": "free/a,free/b"},
                            {"paid/x": E("u", 401, "Unauthorized", None, None),
                             "free/a": "should never run"})
    assert calls == ["paid/x"]
    assert failed


def test_total_failure_marks_the_job_and_claims_no_fallback(monkeypatch, tmp_path):
    calls, md, failed = _run(monkeypatch, tmp_path,
                             {**BASE, "MODEL": "paid/x", "AI_REVIEW_FALLBACK_MODELS": "free/a"},
                             {"paid/x": E("u", 402, "pay", None, None),
                              "free/a": E("u", 429, "rate", None, None)})
    assert calls == ["paid/x", "free/a"]
    assert failed
    assert "FALLBACK MODEL" not in md      # nothing succeeded; claiming one would lie
    assert "request failed" in md


def test_primary_listed_again_in_fallbacks_is_not_retried(monkeypatch, tmp_path):
    calls, _, _ = _run(monkeypatch, tmp_path,
                       {**BASE, "MODEL": "free/a", "AI_REVIEW_FALLBACK_MODELS": "free/a,free/b"},
                       {"free/a": E("u", 402, "pay", None, None), "free/b": "ok"})
    assert calls == ["free/a", "free/b"]


def test_timeout_without_a_status_code_is_retryable(monkeypatch, tmp_path):
    calls, _, _ = _run(monkeypatch, tmp_path,
                       {**BASE, "MODEL": "paid/x", "AI_REVIEW_FALLBACK_MODELS": "free/a"},
                       {"paid/x": TimeoutError("timed out"), "free/a": "ok"})
    assert calls == ["paid/x", "free/a"]


@pytest.mark.parametrize("raw", ["", "   ", "not-a-number"])
def test_unset_or_junk_cap_variable_does_not_crash(monkeypatch, tmp_path, raw):
    """An UNSET Actions variable arrives as '', not as an absent env var -- int('')
    would have crashed the script on its first run after the var was wired in."""
    calls, md, failed = _run(monkeypatch, tmp_path,
                             {**BASE, "MODEL": "paid/x", "AI_REVIEW_FALLBACK_MODELS": "",
                              "AI_REVIEW_CAP_BYTES": raw},
                             {"paid/x": "LGTM"})
    assert calls == ["paid/x"]
    assert not failed
