"""Adoption C: load, validate and merge bifrost-gates.toml.

INERT BY DESIGN. Nothing in the live control flow calls this module. Under
protected_files.json rule 2 a standalone helper that is genuinely uncalled may
ride as non-protected; the moment bifrost_shell.py adds a call site that is a
rule-3 change and needs a full ceremony. Do not import this from bifrost_shell.py
without one.

Three departures from the briefed design, each because the briefed version would
fail in this fleet:

1. **No SIGHUP.** The brief did `signal.signal(signal.SIGHUP, ...)` at import.
   `signal.SIGHUP` does not exist on Windows, so that raises AttributeError and
   takes the module down on the host bifrost_shell.py runs on. Reload is by
   mtime, checked on access -- portable, and no signal handler to install.

2. **Validate before swapping.** The brief assigned straight into the global
   inside the handler, so one malformed edit would replace a working policy with
   a broken one -- or raise inside a signal handler. Here a bad file is rejected
   and the last good config stays live.

3. **An overlay may only tighten.** The brief let a client overlay merge freely.
   That means an overlay could set `ceremony_required = false` and switch off the
   control it exists to enforce. Safety keys move one way only.
"""

import os

try:                                    # 3.11+
    import tomllib as _toml
except ImportError:                     # pragma: no cover - older interpreters
    import tomli as _toml               # type: ignore


class GateError(ValueError):
    """The gate file is unusable. Raised by validate/load, never by a reload."""


# `enabled` mirrors bifrost_shell_policy.json, which is the live authority on
# whether a class runs at all. The risk keys are this file's new dimension.
TOOL_CLASS_KEYS = {
    "enabled": bool,
    "max_risk": float,
    "ceremony_required": bool,
    "review_required": bool,
}

# Safety keys and the direction an overlay may move them.
#   "enable"  -- false->true only (turning a control ON is tightening)
#   "disable" -- true->false only (turning a capability OFF is tightening)
#   "lower"   -- may only decrease
TIGHTEN_ONLY = {
    "ceremony_required": "enable",
    "review_required": "enable",
    "human_approval_required": "enable",
    "enabled": "disable",
    "max_risk": "lower",
    "shieldstral_score_max": "lower",
}


def validate_gates(doc):
    """Return doc unchanged, or raise GateError naming the first real problem."""
    if not isinstance(doc, dict):
        raise GateError("gates document is not a table")
    if doc.get("schema_version") != 1:
        raise GateError("unsupported schema_version: %r" % doc.get("schema_version"))

    classes = doc.get("tool_classes")
    if not isinstance(classes, dict) or not classes:
        raise GateError("tool_classes is missing or empty")
    for name, spec in classes.items():
        if not isinstance(spec, dict):
            raise GateError("tool_class %s is not a table" % name)
        for key, typ in TOOL_CLASS_KEYS.items():
            if key not in spec:
                raise GateError("tool_class %s is missing %s" % (name, key))
            val = spec[key]
            # bool is a subclass of int; check it first or True passes as a float.
            if typ is float:
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise GateError("tool_class %s.%s must be a number" % (name, key))
                if not 0.0 <= float(val) <= 1.0:
                    raise GateError("tool_class %s.%s out of range 0..1: %r"
                                    % (name, key, val))
            elif not isinstance(val, bool):
                raise GateError("tool_class %s.%s must be true/false" % (name, key))

    gates = doc.get("phase_gates") or {}
    if not isinstance(gates, dict):
        raise GateError("phase_gates is not a table")
    for tname, spec in gates.items():
        if not isinstance(spec, dict):
            raise GateError("phase_gate %s is not a table" % tname)
        if "_to_" not in tname:
            raise GateError("phase_gate %s is not named <from>_to_<to>" % tname)
    return doc


def load_gates(path):
    """Parse and validate. Raises GateError for both a parse and a schema failure,
    so a caller has exactly one thing to catch."""
    try:
        with open(path, "rb") as fh:
            doc = _toml.load(fh)
    except OSError as exc:
        raise GateError("cannot read %s: %s" % (path, exc))
    except Exception as exc:                       # tomllib.TOMLDecodeError
        raise GateError("cannot parse %s: %s" % (path, exc))
    return validate_gates(doc)


def _tighter(key, base_val, over_val):
    """May the overlay set this key to over_val? Non-safety keys are free."""
    rule = TIGHTEN_ONLY.get(key)
    if rule is None:
        return True
    if rule == "enable":
        return bool(over_val) or not bool(base_val)      # false->true, or unchanged
    if rule == "disable":
        return (not bool(over_val)) or bool(base_val)    # true->false, or unchanged
    if rule == "lower":
        try:
            return float(over_val) <= float(base_val)
        except (TypeError, ValueError):
            return False
    return False


def merge_overlay(base, overlay, strict=True):
    """Deep-merge overlay over base. Safety keys may only tighten.

    strict=True raises on a loosening attempt. strict=False drops it and reports
    it -- used when a client overlay should never be able to break the service,
    only to be ignored and logged.
    """
    rejected = []

    def walk(b, o, trail):
        out = dict(b)
        for k, v in (o or {}).items():
            if isinstance(v, dict) and isinstance(b.get(k), dict):
                out[k] = walk(b[k], v, trail + [k])
            else:
                if k in TIGHTEN_ONLY and k in b and not _tighter(k, b[k], v):
                    where = ".".join(trail + [k])
                    if strict:
                        raise GateError(
                            "overlay may not loosen %s: %r -> %r" % (where, b[k], v))
                    rejected.append((where, b[k], v))
                    continue
                out[k] = v
        return out

    merged = walk(base, overlay, [])
    return (merged, rejected)


class GateStore:
    """Holds the last GOOD config and reloads it when the file's mtime moves.

    A failed reload keeps the previous config and records why. That is the whole
    point: a policy file people edit by hand must not be able to take the tool
    surface down, and it must not silently fall back to 'no gates' either.
    """

    def __init__(self, path, overlay_path=None):
        self.path = path
        self.overlay_path = overlay_path
        self._mtime = None
        self._gates = None
        self.last_error = None
        self.rejected_overlay_keys = []
        self.reload_count = 0

    def _stamp(self):
        try:
            s = os.stat(self.path).st_mtime_ns
        except OSError:
            return None
        if self.overlay_path:
            try:
                s = (s, os.stat(self.overlay_path).st_mtime_ns)
            except OSError:
                s = (s, None)
        return s

    def load(self):
        """Force a load. Raises on first load; later failures are non-fatal."""
        gates = load_gates(self.path)
        if self.overlay_path:
            over = load_gates_overlay(self.overlay_path)
            gates, rejected = merge_overlay(gates, over, strict=False)
            self.rejected_overlay_keys = rejected
        self._gates = gates
        self._mtime = self._stamp()
        self.last_error = None
        self.reload_count += 1
        return gates

    def gates(self):
        """Current config, reloading only if the file actually changed."""
        if self._gates is None:
            return self.load()
        stamp = self._stamp()
        if stamp != self._mtime:
            try:
                return self.load()
            except GateError as exc:
                # Keep serving the last good config; remember the mtime so a
                # broken file is not re-parsed on every single call.
                self.last_error = str(exc)
                self._mtime = stamp
        return self._gates

    def tool_class(self, name):
        return (self.gates().get("tool_classes") or {}).get(name, {})

    def phase_gate(self, transition):
        return (self.gates().get("phase_gates") or {}).get(transition, {})


def load_gates_overlay(path):
    """An overlay is a partial document, so it is parsed but not schema-checked.

    A MISSING file is not an error -- it means "no overlay". Measured 2026-09-21:
    raising here made GateStore.load() raise, which _gate_denies caught and turned
    into False, so pointing BIFROST_GATES_OVERLAY at a not-yet-created file
    silently disabled the whole gate layer. Arming a switch must never disarm one.

    An overlay that exists but cannot be read or parsed is a different thing and
    still raises: that is a file someone MEANT to apply, and it must be loud.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as fh:
            return _toml.load(fh)
    except OSError as exc:
        raise GateError("cannot read overlay %s: %s" % (path, exc))
    except Exception as exc:
        raise GateError("cannot parse overlay %s: %s" % (path, exc))
