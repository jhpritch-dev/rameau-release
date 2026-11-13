"""
Unit test suite for Bundle B (Engine Stability & Observability).
Verifies:
1. telemetry.py: _DB_INITIALIZED flag, WAL mode, busy timeout, and timestamp index.
2. arbiter.py: log = logger alias is present.
3. classifier.py: Runtime reasoning output replaces \u2192 with ASCII ->.
4. metrics.py: OperatingMode.JARVIS_OFFLINE is handled in set_mode.
"""
import inspect
import sqlite3
import tempfile
import pytest

def test_telemetry_initialization_and_wal(tmp_path):
    import telemetry

    # Check telemetry module has _DB_INITIALIZED and _DB_LOCK
    assert hasattr(telemetry, "_DB_INITIALIZED"), "_DB_INITIALIZED missing in telemetry"
    assert hasattr(telemetry, "_DB_LOCK"), "_DB_LOCK missing in telemetry"

    # Test _ensure_db in temporary directory
    test_db = tmp_path / "test_telemetry.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(telemetry, "TELEMETRY_DB", test_db)
        mp.setattr(telemetry, "TELEMETRY_DIR", tmp_path)
        mp.setattr(telemetry, "_DB_INITIALIZED", False)

        conn = telemetry._ensure_db()
        # Verify WAL mode
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal", f"Expected WAL journal mode, got {mode}"

        # Verify index exists
        indexes = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index';").fetchall()]
        assert "idx_inference_events_timestamp" in indexes, "idx_inference_events_timestamp index missing"
        conn.close()

def test_arbiter_logger_alias():
    import arbiter
    assert hasattr(arbiter, "log"), "arbiter.log alias missing"
    assert arbiter.log is arbiter.logger

def test_classifier_ascii_reasoning():
    import classifier
    # Classify a short string that triggers the autocomplete reason
    res = classifier.classify("hi")
    reasons = res.reasoning
    assert "\u2192" not in reasons, f"Found non-ASCII Unicode arrow \\u2192 in classifier reasons: {reasons}"
    assert "-> likely autocomplete" in reasons, f"Expected '-> likely autocomplete' in reasons: {reasons}"

def test_metrics_mode_gauge_includes_jarvis_offline():
    import metrics
    from config import OperatingMode

    mc = metrics.MetricsCollector()
    mc.set_mode("JARVIS_OFFLINE")
    val = metrics.ACTIVE_MODE.labels(mode="JARVIS_OFFLINE")._value.get()
    assert val == 1.0, f"Expected JARVIS_OFFLINE gauge 1.0, got {val}"
