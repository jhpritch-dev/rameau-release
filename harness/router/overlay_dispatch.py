"""
overlay_dispatch.py - Per-(profile, model) overlay dispatch table

Architectural origin: 2026-05-16 LAYER-E-BENCH-2 finding (memory #136).
IGOR_BASELINE overlay effect is non-uniform per model. Path D = parallel
dispatch table replacing the prior bifrost-igor-only injection.

Safety boundary: this module is the canonical site for overlay routing
decisions. Overlay TEXT is owned by main.py (where IGOR_BASELINE is
defined); this module decides WHICH (profile, model) pairs receive it.

Empirical findings backing the table (LAYER-E-BENCH-2, 2026-05-16):
- bifrost-igor / gpt-oss:120b: existing overlay -> correct hedging behavior.
  Preserve. Confirmed for /coding, /general, /research profiles.
- gemma3:12b: with overlay -> acknowledges uncertainty cleanly. Dark-horse
  beneficiary. Apply.
- phi4:14b: with overlay -> procedure hallucination (#137). MUST NOT receive.
  Explicit deny.
- All other models: default deny pending empirical evidence.
"""

from typing import Optional

# Dispatch table: (profile_name, model_id) -> "apply" | "skip"
# Wildcard "*" for profile means "all profiles".
# Models NOT in this table default to skip (no overlay applied).
#
# Architect decision locked 2026-05-17 (FLEET-PROFILE-1):
#   - bifrost-igor + gpt-oss:120b: apply (preserve incumbent)
#   - gemma3:12b: apply (BENCH-2 dark-horse)
#   - phi4:14b: explicit deny (BENCH-2 procedure hallucination, #137)
#   - wildcard default: deny
#
# 2026-08-11 (task #77): the raw-model key read "gpt-oss:120b" until today. No such model
# exists on the fleet -- it is gpt-oss-igor-65k on Forge -- so that entry could never match,
# and a caller naming the raw model fell through to the wildcard default (deny) and lost the
# overlay with no error and no log line. The alias keys above were unaffected, which is why
# nothing was observably broken. RENAMED, not duplicated: a hit on the old string would mean
# a dead model name is still live somewhere else, and that should fail rather than be
# absorbed here. The 2026-05-17 decision above is unchanged -- same model, new name.
# KNOWN GAP, accepted: main.py:1523 does `model_hint = body.get("model", "")` with no
# normalization, so lookup is an exact string match. Ollama's canonical tag is
# "gpt-oss-igor-65k:latest"; a caller who spells it that way still misses. Deliberately not
# papered over with a second key -- fix it with normalization if it ever bites.
_DISPATCH = {
    ("*", "bifrost-igor"):      "apply",
    ("*", "bifrost-igor-solo"): "apply",  # 2026-06-04: overnight decomposer = solo 65k; same base, same overlay
    ("*", "gpt-oss-igor-65k"):  "apply",  # renamed 2026-08-11, see note above
    ("*", "gemma3:12b"):        "apply",
    ("*", "phi4:14b"):          "skip",
}


def should_apply_overlay(profile_name: Optional[str], model_id: str) -> bool:
    """Return True if the overlay should be applied for this (profile, model).

    Resolution order:
      1. Exact (profile_name, model_id) match.
      2. Wildcard ("*", model_id) fallback.
      3. Default: skip (no overlay).
    """
    if not model_id:
        return False
    if profile_name and (profile_name, model_id) in _DISPATCH:
        return _DISPATCH[(profile_name, model_id)] == "apply"
    if ("*", model_id) in _DISPATCH:
        return _DISPATCH[("*", model_id)] == "apply"
    return False


def apply_overlay(profile_name: Optional[str], model_id: str,
                  base_prompt: str, overlay_text: str) -> str:
    """Conditionally append overlay_text to base_prompt per dispatch table."""
    if should_apply_overlay(profile_name, model_id):
        return base_prompt + "\n\n" + overlay_text
    return base_prompt
