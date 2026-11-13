"""The cloud-egress gate must redact bare high-entropy hex, and must NOT redact git SHA-1.

Written 2026-10-06 alongside the HEXSECRET port. Before this file, ZERO of the 113 test files in
this repo named sanitize_messages_secrets -- the outbound credential gate had no coverage at all.

The gate under test is sanitize_messages_secrets, NOT sanitize_outbound. That distinction is the
point: the gate filters on _SECRET_KINDS, so a pattern added to _PATTERNS without registering its
kind is INERT at the cloud boundary while still passing any test that calls the regex directly.
test_kind_is_registered_in_secret_kinds and the gate-level tests exist to catch exactly that.

The 40-character exclusion is deliberate and is asserted here as a discriminator, not an
afterthought: a git SHA-1 is not a secret, and an implementation that simply redacts [0-9a-f]{40,}
passes every masking test above while breaking commit hashes in cloud context. That was the real
2026-08-04 regression on the sibling boundary; this file refuses to let it recur here.
"""
import pytest

import egress_guard

SHA1_GIT = "e912465a1b2c3d4e5f60718293a4b5c6d7e8f901"          # exactly 40 -- a commit hash
HEX_41 = "a" * 41                                                # one above the exclusion
HEX_48 = "0123456789abcdef0123456789abcdef0123456789abcdef"      # 48
HEX_64 = "f" * 64                                                # SHA-256 length -- the real risk
ANTHROPIC = "sk-ant-api03-" + "A" * 40                           # control: already masked
AWS = "AKIA" + "A" * 16                                          # control
SLACK = "xoxb-" + "1" * 20                                       # control


def gate(text):
    """Run the REAL outbound gate over one user message; return (masked_text, n_masked)."""
    msgs, mapping, n = egress_guard.sanitize_messages_secrets(
        [{"role": "user", "content": text}])
    return msgs[0]["content"], n, mapping


# --- registration: the thing a regex-only test cannot see -------------------------------

def test_kind_is_registered_in_secret_kinds():
    """A pattern in _PATTERNS is inert unless its kind is in _SECRET_KINDS."""
    kinds = {k for k, _ in egress_guard._PATTERNS}
    assert "HEXSECRET" in kinds, "HEXSECRET missing from _PATTERNS"
    assert "HEXSECRET" in egress_guard._SECRET_KINDS, (
        "HEXSECRET is in _PATTERNS but NOT in _SECRET_KINDS -- the outbound gate filters on "
        "that set, so the pattern never runs at the cloud boundary")


def test_hex_pattern_is_last_so_specific_formats_claim_their_tokens_first():
    kinds = [k for k, _ in egress_guard._PATTERNS]
    assert kinds[-1] == "HEXSECRET", (
        "bare hex is the least specific pattern and must sort last, or it can swallow a token "
        "a narrower pattern should own; order is %r" % (kinds,))


# --- seen to pass: real secrets are masked BY THE GATE -----------------------------------

@pytest.mark.parametrize("value,label", [(HEX_41, "41"), (HEX_48, "48"), (HEX_64, "64")])
def test_bare_hex_is_masked_by_the_outbound_gate(value, label):
    out, n, _ = gate("the token is %s please use it" % value)
    assert value not in out, "%s-char bare hex crossed to cloud unmasked" % label
    assert n >= 1


def test_masking_is_reversible_through_the_mapping():
    out, _, mapping = gate("key %s end" % HEX_64)
    assert HEX_64 in mapping.values()
    restored = out
    for ph, original in mapping.items():
        restored = restored.replace(ph, original)
    assert restored == "key %s end" % HEX_64


# --- the discriminator: a git SHA-1 must SURVIVE -----------------------------------------

def test_git_sha1_is_NOT_masked():
    """Exactly 40 hex is a commit hash. Redacting it was the 2026-08-04 regression."""
    out, n, _ = gate("deployed commit %s to prod" % SHA1_GIT)
    assert SHA1_GIT in out, "a 40-char git SHA-1 was redacted -- commit hashes must stay readable"
    assert n == 0


def test_sha1_survives_in_the_same_message_as_a_real_secret():
    """The hard case: an implementation that bails on the whole string fails here."""
    out, n, _ = gate("commit %s used key %s" % (SHA1_GIT, HEX_64))
    assert SHA1_GIT in out, "the commit hash was collateral damage"
    assert HEX_64 not in out, "the real secret escaped"
    assert n >= 1


# --- cardinality: 0, 1, many -------------------------------------------------------------

def test_zero_hex_masks_nothing():
    out, n, _ = gate("a perfectly ordinary sentence with no secrets in it")
    assert n == 0
    assert out == "a perfectly ordinary sentence with no secrets in it"


def test_many_distinct_hex_values_are_all_masked():
    a, b, c = "1" * 64, "2" * 64, "3" * 50
    out, n, _ = gate("%s then %s then %s" % (a, b, c))
    for v in (a, b, c):
        assert v not in out
    assert n >= 3


def test_repeated_identical_secret_reuses_one_placeholder():
    out, _, mapping = gate("%s and again %s" % (HEX_64, HEX_64))
    assert HEX_64 not in out
    assert sum(1 for v in mapping.values() if v == HEX_64) == 1


# --- controls: the pre-existing classes must not regress ---------------------------------

@pytest.mark.parametrize("value", [ANTHROPIC, AWS, SLACK])
def test_prefixed_credentials_still_masked(value):
    out, n, _ = gate("credential %s here" % value)
    assert value not in out
    assert n >= 1


def test_bearer_still_masked():
    out, n, _ = gate("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")  # rameau-scan: allow -- synthetic bearer fixture: the egress guard must detect it
    assert "abcdefghijklmnopqrstuvwxyz123456" not in out
    assert n >= 1


def test_ordinary_short_hex_is_untouched():
    """A floor exists for a reason -- an 8-char hex color or id is not a secret."""
    out, n, _ = gate("the color is deadbeef and the id is cafe1234")
    assert "deadbeef" in out and "cafe1234" in out
    assert n == 0
