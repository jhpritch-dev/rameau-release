"""CEREMONY 121: a valid X-Complexity-Hint wins over a confident NPU in live mode.

classify() honors the hint first ("trust the caller if they say so"); the v98 wrapper's live NPU-wins
branch then replaced that band while keeping hint_override=True. The NPU is stubbed. The hint case FAILS
on b65fed8 (the 2026-10-13 audit's pre-tree run); the rest pass on both trees.
"""

import asyncio
import types

import config

PROMPT = "Explain how a hash map works."


def _npu(monkeypatch, band, mode="live", conf=0.99):
    import classifier
    import npu_classifier
    calls = []

    async def fake_npu(prompt):
        calls.append(prompt)
        return types.SimpleNamespace(prediction=band, confidence=conf), None

    monkeypatch.setattr(npu_classifier, "npu_classify_ex", fake_npu)
    monkeypatch.setattr(classifier, "NPU_ROUTING_ENABLED", mode)
    return classifier, calls


def test_hint_wins_over_a_confident_npu(monkeypatch):
    classifier, calls = _npu(monkeypatch, config.ComplexityBand.MODERATE)
    res, meta = asyncio.run(classifier.classify_with_npu(PROMPT, 0, "complex"))
    assert res.band == config.ComplexityBand.COMPLEX, "hint=complex was replaced by the NPU's %s" % res.band.value
    assert res.hint_override is True
    assert meta["routing_source"] == "hint"
    assert calls == [], "the NPU was asked although the caller named the band"


def test_without_a_hint_the_npu_still_wins(monkeypatch):
    classifier, calls = _npu(monkeypatch, config.ComplexityBand.FRONTIER)
    res, meta = asyncio.run(classifier.classify_with_npu(PROMPT, 0, None))
    assert res.band == config.ComplexityBand.FRONTIER and meta["routing_source"] == "npu" and len(calls) == 1


def test_an_invalid_hint_is_ignored(monkeypatch):
    classifier, calls = _npu(monkeypatch, config.ComplexityBand.FRONTIER)
    res, meta = asyncio.run(classifier.classify_with_npu(PROMPT, 0, "urgent"))
    assert res.hint_override is False and meta["routing_source"] == "npu" and len(calls) == 1


def test_npu_off_is_unchanged(monkeypatch):
    classifier, calls = _npu(monkeypatch, config.ComplexityBand.MODERATE, mode="off")
    res, meta = asyncio.run(classifier.classify_with_npu(PROMPT, 0, "complex"))
    assert res.band == config.ComplexityBand.COMPLEX and meta["routing_source"] == "rule" and calls == []


def test_shadow_is_unchanged(monkeypatch):
    classifier, calls = _npu(monkeypatch, config.ComplexityBand.MODERATE, mode="shadow")
    res, meta = asyncio.run(classifier.classify_with_npu(PROMPT, 0, "complex"))
    assert res.band == config.ComplexityBand.COMPLEX and meta["routing_source"] == "rule"
    assert meta["npu_pred"] == "MODERATE" and len(calls) == 1
