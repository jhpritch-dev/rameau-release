"""
Unit test suite for Bundle A (Security & Policy Boundaries).
Verifies:
1. main.py: _air_gapped() automatically enforces offline modes, and _t3_affinity_tier respects air-gap.
2. bifrost_shell.py: read_file, grep_repo, list_dir, git_read use READ_FS gate.
3. autopilot_graph.py: _is_cloud_tier helper handles all 3-* tiers, and WORKSHOP_OFFLINE clamps to 1a-coder.
"""
import pytest
from unittest.mock import patch, MagicMock

def test_main_air_gapped_offline_modes():
    import main
    from config import OperatingMode, settings

    # In regular mode with _AIR_GAPPED=False, air gap is False
    with patch.object(main, "_AIR_GAPPED", False):
        with patch.object(settings, "current_mode", OperatingMode.JARVIS):
            assert main._air_gapped() is False

        # JARVIS_OFFLINE enforces air-gap automatically
        with patch.object(settings, "current_mode", OperatingMode.JARVIS_OFFLINE):
            assert main._air_gapped() is True

        # WORKSHOP_OFFLINE enforces air-gap automatically
        with patch.object(settings, "current_mode", OperatingMode.WORKSHOP_OFFLINE):
            assert main._air_gapped() is True

def test_main_t3_affinity_air_gapped_guard():
    import main
    from config import OperatingMode, settings

    # When air gapped via mode, T3 affinity must return None
    with patch.object(settings, "current_mode", OperatingMode.JARVIS_OFFLINE):
        res = main._t3_affinity_tier(
            prompt="what is the latest news today?",
            band=None,
            active_tiers={"3-perplexity", "1a-coder"},
            carries_own_source=False,
        )
        assert res is None, "T3 affinity must return None when air-gapped"

def test_shell_read_fs_gating():
    # Read bifrost_shell.py source and verify tool decorators
    import inspect

    # bifrost_shell imports fastmcp at module scope, and fastmcp is in neither
    # requirements.txt nor requirements-dev.txt on purpose -- it cannot co-install with the
    # core pins, which requirements-mcp.txt records as a measured ResolutionImpossible. So
    # CI will not have it, and this test would be the one red in an otherwise green run.
    pytest.importorskip("fastmcp", reason="shell-MCP dependency, not pinned in this release")
    import bifrost_shell

    # Check that read_file, grep_repo, list_dir, git_read have READ_FS gated wrapper or decorator
    src = inspect.getsource(bifrost_shell)
    # Check that read_file is decorated with @gated_tool("READ_FS")
    assert '@gated_tool("READ_FS")\nasync def read_file' in src or '@gated_tool("READ_FS")\r\nasync def read_file' in src
    assert '@gated_tool("READ_FS")\nasync def grep_repo' in src or '@gated_tool("READ_FS")\r\nasync def grep_repo' in src
    assert '@gated_tool("READ_FS")\nasync def list_dir' in src or '@gated_tool("READ_FS")\r\nasync def list_dir' in src
    assert '@gated_tool("READ_FS")\nasync def git_read' in src or '@gated_tool("READ_FS")\r\nasync def git_read' in src

def test_autopilot_cloud_tier_detection_and_clamping():
    import autopilot_graph
    from config import OperatingMode, settings

    # Helper _is_cloud_tier must exist and identify 3-* prefixes
    assert hasattr(autopilot_graph, "_is_cloud_tier"), "_is_cloud_tier function missing"
    assert autopilot_graph._is_cloud_tier("3-claude") is True
    assert autopilot_graph._is_cloud_tier("3-gemini") is True
    assert autopilot_graph._is_cloud_tier("3-custom-tier") is True
    assert autopilot_graph._is_cloud_tier("1a-coder") is False
    assert autopilot_graph._is_cloud_tier("2.5") is False

    # Clamping in WORKSHOP_OFFLINE must clamp to 1a-coder
    with patch.object(settings, "current_mode", OperatingMode.WORKSHOP_OFFLINE):
        clamped = autopilot_graph._clamp_tier_for_mode("3-claude")
        assert clamped == "1a-coder"

    # Clamping in JARVIS_OFFLINE must clamp to 2.5
    with patch.object(settings, "current_mode", OperatingMode.JARVIS_OFFLINE):
        clamped = autopilot_graph._clamp_tier_for_mode("3-claude")
        assert clamped == "2.5"
