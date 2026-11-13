"""CEREMONY 119: chat slash commands no longer write process-wide router settings.

`/review on|off` set settings.default_strategy -- the same control state POST /strategy guards with the
X-Bifrost-Control token (fail-closed) -- from any chat message, with no token. `/bifrost <profile>` wrote
settings.bifrost_profile the same way. The write cases FAIL on b65fed8 (the 2026-10-13 audit's pre-tree run).
"""

import asyncio
import types

import config


def test_review_on_does_not_change_the_global_strategy():
    import commands
    s = config.settings
    before = s.default_strategy
    try:
        out = asyncio.run(commands.handle_command("/review on", s, types.SimpleNamespace(), None))
        assert s.default_strategy == before, "chat '/review on' set default_strategy=%s with no token" % s.default_strategy.value
        assert "POST /strategy" in out
    finally:
        s.default_strategy = before


def test_review_off_does_not_change_the_global_strategy():
    import commands
    s = config.settings
    before = s.default_strategy
    try:
        s.default_strategy = config.RoutingStrategy.TWO_PASS
        asyncio.run(commands.handle_command("/review off", s, types.SimpleNamespace(), None))
        assert s.default_strategy == config.RoutingStrategy.TWO_PASS, "chat '/review off' wrote default_strategy"
    finally:
        s.default_strategy = before


def test_bifrost_profile_is_not_written_from_chat():
    import commands
    s = config.settings
    before = s.bifrost_profile
    arg = "light" if before == config.BifrostProfile.B_HEAVY else "heavy"
    try:
        asyncio.run(commands.handle_command("/bifrost " + arg, s, types.SimpleNamespace(), None))
        assert s.bifrost_profile == before, "chat '/bifrost %s' wrote bifrost_profile" % arg
    finally:
        s.bifrost_profile = before


def test_read_only_commands_still_answer():
    import commands
    out = asyncio.run(commands.handle_command("/bifrost", config.settings, types.SimpleNamespace(), None))
    assert "Bifrost Profile" in out
    bad = asyncio.run(commands.handle_command("/review sideways", config.settings, types.SimpleNamespace(), None))
    assert bad.startswith("Invalid argument")
