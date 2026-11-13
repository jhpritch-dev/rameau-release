"""A kill switch with nothing to get wrong.

STAGED, rule 2: nothing imports this yet.

WHY THIS EXISTS RATHER THAN REUSING THE OVERLAY. bifrost-gates.toml's overlay can
tighten a class, and it does reach a running process -- that was measured, not assumed.
But it is TOML, and a kill switch made of TOML has two failure modes that were also
measured, on 2026-09-21, against the live bifrost_gates.py:

  1. Pointing BIFROST_GATES_OVERLAY at a file that does not exist yet makes every gate
     lookup raise GateError. _gate_denies catches that and returns False. So the
     natural way to arm a switch -- set the variable now, write the file when you need
     it -- silently makes the ENTIRE gate layer inert.
  2. An unparsable overlay falls back to the base config, so a typo in an emergency
     deny-overlay means the deny silently does not happen. last_error is set; nothing
     reads it.

Both are fail-OPEN, which is the wrong direction for the one control you reach for
when something is going wrong. So the emergency path is separated from the graduated
one: the overlay keeps doing careful, typo-tolerant tightening, and this module does
the blunt stop. It parses nothing. There is no syntax to get wrong.

HOW TO USE IT
    Arm:     set BIFROST_TOOLS_HALT=1        (or: create the file TOOLS-HALT)
    Disarm:  unset it                        (or: delete/rename the file)

The FILE form matters: it works when you cannot restart a process to change its
environment, and it can be dropped in over a share by someone who is not at a console.
The ENV form matters: it survives a filesystem that has gone read-only or unreachable.
Either alone is sufficient. Neither can be partially applied.

WHAT IT DOES NOT DO. It never grants. It is consulted alongside the policy and the
gates, and like them it can only deny. Turning it off restores whatever the policy and
gates already said -- it does not re-enable anything they withhold.
"""
from __future__ import annotations

import os

HALT_ENV = "BIFROST_TOOLS_HALT"
HALT_FILE_ENV = "BIFROST_TOOLS_HALT_FILE"
DEFAULT_HALT_FILENAME = "TOOLS-HALT"

#: Values that do NOT arm the switch. Everything else does, including "false".
#:
#: This is deliberately inverted from the usual truthy parsing. A switch whose job is
#: to stop things must not be talked out of it by an unexpected value: if someone sets
#: BIFROST_TOOLS_HALT to "yes please" or "TRUE" or "1 ", the intent is obvious and the
#: switch arms. Only an explicitly empty or explicitly negative value stands down, and
#: "false"/"0"/"off"/"no" are spelled out so that setting it to one of those is a
#: deliberate disarm rather than an accident.
_DISARMED = frozenset({"", "0", "false", "off", "no"})


def _halt_file_path(base_dir=None):
    """Where the sentinel file lives.

    BIFROST_TOOLS_HALT_FILE overrides it outright. Otherwise it sits next to the gates
    file, because that is the directory an operator already associates with turning
    things off.
    """
    override = os.getenv(HALT_FILE_ENV)
    if override:
        return override
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, DEFAULT_HALT_FILENAME)


def _env_armed():
    raw = os.getenv(HALT_ENV)
    if raw is None:
        return False
    return raw.strip().lower() not in _DISARMED


def _file_armed(base_dir=None):
    try:
        return os.path.exists(_halt_file_path(base_dir))
    except Exception:                                   # noqa: BLE001
        # An unreadable path is not a reason to halt, and not a reason to crash the
        # tool surface either. The env form is the backstop for exactly this case.
        return False


def halted(base_dir=None):
    """True if the fleet tool surface is halted. Never raises.

    Never raising is the point. This is called from inside the tool gate, so an
    exception here would propagate into every tool call -- and the existing handler
    catches exceptions and continues, which would turn a crash into a silent disarm.
    """
    try:
        return _env_armed() or _file_armed(base_dir)
    except Exception:                                   # noqa: BLE001
        return False


def reason(base_dir=None):
    """Why the halt is active, for the denial message. Empty string when it is not.

    Named so the operator can tell WHICH form armed it -- otherwise disarming means
    guessing whether to unset a variable or remove a file, and a half-disarm looks
    exactly like a switch that did not work.
    """
    try:
        parts = []
        if _env_armed():
            parts.append("%s is set" % HALT_ENV)
        if _file_armed(base_dir):
            parts.append("sentinel file %s exists" % _halt_file_path(base_dir))
        return "; ".join(parts)
    except Exception:                                   # noqa: BLE001
        return ""
