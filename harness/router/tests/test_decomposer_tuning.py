"""
Guards for the v218 decomposer tuning (tune AUTOPILOT to the model it ACTUALLY runs on).

AUTOPILOT's decomposer was written when _DECOMPOSER_TIER served T2 gemma4:26b. It now
serves 2.5-solo = gpt-oss-igor-65k (gpt-oss:120b), and the code never caught up:

  - It stripped <think>...</think> (R1/Qwen3 syntax). gpt-oss emits the Harmony
    analysis/final channel format instead, so the strip was a no-op against the live model.
  - It passed no `format`, so a 120B model free-sampled its JSON. Measured on v217:
    3 of 4 decompose attempts died on "Expecting ',' delimiter" at char ~1000-1500.
  - It passed no temperature, taking the 0.7 default on a strict-JSON task.

These tests pin the behavior, not the prose.
"""
import ast
import inspect
import pathlib

import pytest

from autopilot_graph import _strip_reasoning_preamble

SRC = pathlib.Path(__file__).resolve().parent.parent / "autopilot_graph.py"
OLLAMA_SRC = pathlib.Path(__file__).resolve().parent.parent / "backends" / "ollama.py"


# --- reasoning-preamble stripping -------------------------------------------------

def test_plain_json_untouched():
    assert _strip_reasoning_preamble('{"subtasks": []}') == '{"subtasks": []}'


def test_r1_think_block_stripped():
    assert _strip_reasoning_preamble('<think>plan</think>{"subtasks": [1]}') == '{"subtasks": [1]}'


def test_gpt_oss_harmony_channel_stripped():
    """The live decomposer model's actual dialect -- this is the regression."""
    raw = (
        "<|channel|>analysis<|message|>Let me think about the subtasks...<|end|>"
        "<|start|>assistant<|channel|>final<|message|>{\"subtasks\": [2]}"
    )
    assert _strip_reasoning_preamble(raw) == '{"subtasks": [2]}'


def test_harmony_without_analysis_channel():
    assert _strip_reasoning_preamble('<|channel|>final<|message|>{"a": 1}') == '{"a": 1}'


def test_residual_control_tokens_removed():
    assert "<|" not in _strip_reasoning_preamble('<|channel|>final<|message|>{"a":1}<|end|>')


@pytest.mark.parametrize("bad", ["", None])
def test_empty_and_none_are_safe(bad):
    assert _strip_reasoning_preamble(bad) == ""


def test_last_final_channel_wins():
    """Multiple channels: payload follows the LAST final marker."""
    raw = (
        "<|channel|>analysis<|message|>draft {\"subtasks\": [\"WRONG\"]}<|end|>"
        "<|start|>assistant<|channel|>final<|message|>{\"subtasks\": [\"RIGHT\"]}"
    )
    assert "WRONG" not in _strip_reasoning_preamble(raw)
    assert "RIGHT" in _strip_reasoning_preamble(raw)


# --- the decompose call must constrain its output ---------------------------------

def _decompose_async_src():
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_decompose_async":
            return ast.get_source_segment(src, node) or ""
    pytest.fail("_decompose_async not found")


def _dispatch_kwarg(name):
    """The `name=` kwarg node on the _dispatch_tier_chat call inside _decompose_async.

    Read from the AST, not the source text -- a substring check also matches the word in
    a comment, which is exactly how the first version of this test fooled itself.
    """
    tree = ast.parse(_decompose_async_src())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_dispatch_tier_chat":
            for kw in node.keywords:
                if kw.arg == name:
                    return kw.value
    return None


def test_decompose_requests_grammar_constrained_output():
    """Without a grammar the model free-samples: 3 of 4 attempts returned invalid JSON."""
    assert _dispatch_kwarg("format") is not None, "decompose must pass a `format=`"


def test_decompose_pins_temperature_to_zero():
    assert "temperature=0.0" in _decompose_async_src(), (
        "decompose must pin temperature=0.0; the dispatch default is 0.7, which drifts "
        "mid-JSON on a strict-schema task."
    )


def test_decompose_strips_reasoning_via_helper():
    assert "_strip_reasoning_preamble" in _decompose_async_src()


# --- format must survive the native /api/chat payload rebuild ---------------------

def test_ollama_backend_accepts_format():
    from backends.ollama import ollama_chat_completion
    assert "format" in inspect.signature(ollama_chat_completion).parameters


def test_format_forwarded_to_native_builders():
    """Both native builders rebuild the payload from scratch.

    An un-copied key is silently dropped -- the caller then believes it asked for
    constrained JSON and gets free-sampled output. This is the same class of bug the
    tools/num_ctx passthrough comments in that file already document.
    """
    text = OLLAMA_SRC.read_text(encoding="utf-8")
    forwards = text.count('native_payload["format"] = payload["format"]')
    builders = text.count("native_payload = {")
    assert forwards == builders, (
        f"format forwarded in {forwards} native builder(s) but {builders} exist -- "
        "an un-forwarded builder silently drops it"
    )


# --- the decompose grammar (v218) -------------------------------------------------

def test_schema_enforces_minimum_two_subtasks():
    """"Never collapse to a single subtask" must be grammar, not prose.

    Bare format="json" fixed validity but not shape: gpt-oss:120b returned 3 subtasks on
    one call and 1 on the next, same input, temperature 0.
    """
    from autopilot_graph import _decompose_schema
    s = _decompose_schema(require_function_name=True)
    assert s["properties"]["subtasks"]["minItems"] == 2


def test_schema_restricts_tiers_to_the_real_fleet():
    """"assigned_tier MUST be exactly ... No other value is allowed." """
    from autopilot_graph import _decompose_schema
    s = _decompose_schema(require_function_name=True)
    enum = s["properties"]["subtasks"]["items"]["properties"]["assigned_tier"]["enum"]
    # 2026-07-23 Option-B: 2_qwen_coder (qwen3-coder 30B, Forge) added so the code
    # specialist can take the heavy implementation leg, co-resident with 2_devstral.
    assert enum == ["1b", "1a-hearth", "2_devstral", "2_qwen_coder"]
    assert "1a-coder" not in enum  # the stale _SubtaskDef default; prompt forbids it
    assert "2.5" not in enum and "3-claude" not in enum  # no escalation/cloud tiers as leg targets


def test_code_tasks_require_function_name():
    """Regression: a schema without function_name strips it, and the spec builder then
    defaults every leg to "_doc_section" -- resurrecting the v215 prose-essay bug."""
    from autopilot_graph import _decompose_schema
    s = _decompose_schema(require_function_name=True)
    item = s["properties"]["subtasks"]["items"]
    assert "function_name" in item["properties"]
    assert "function_name" in item["required"]


def test_document_tasks_do_not_require_function_name():
    from autopilot_graph import _decompose_schema
    s = _decompose_schema(require_function_name=False)
    item = s["properties"]["subtasks"]["items"]
    assert "function_name" not in item["required"]


def test_schema_carries_every_field_the_spec_builder_reads():
    """_SubtaskDef drifted from the spec builder; this schema must not."""
    from autopilot_graph import _decompose_schema
    props = _decompose_schema(require_function_name=True)["properties"]["subtasks"]["items"]["properties"]
    for f in ("subtask_id", "description", "acceptance_criteria", "assigned_tier",
              "complexity_band", "dependencies", "think",
              "function_name", "input_signature", "output_signature"):
        assert f in props, f"spec builder reads {f!r} but the grammar cannot emit it"


def test_decompose_passes_the_schema_not_bare_json():
    """format must be the schema call, not the string "json".

    Bare "json" constrains validity but not shape, so a 1-subtask answer is legal and the
    three-leg fan-out dies. Checked via AST: `format="json"` also appears in a comment
    directly above this call, which a substring check would happily match.
    """
    fmt = _dispatch_kwarg("format")
    assert fmt is not None, "decompose must pass a `format=`"
    assert not isinstance(fmt, ast.Constant), (
        f"format is the constant {getattr(fmt, 'value', fmt)!r}; bare 'json' permits a "
        "1-subtask answer. Pass _decompose_schema(...) so minItems is enforced."
    )
    assert isinstance(fmt, ast.Call) and getattr(fmt.func, "id", "") == "_decompose_schema"


def test_decompose_temperature_is_pinned_via_ast():
    temp = _dispatch_kwarg("temperature")
    assert isinstance(temp, ast.Constant) and temp.value == 0.0


# --- schema property ORDER is load-bearing (v225) -----------------------------------

# The order gpt-oss actually emits, observed in the raw decompose output on v224.
_MODEL_EMISSION_ORDER = [
    "subtask_id", "description", "assigned_tier",
    "function_name", "input_signature", "output_signature",
    "acceptance_criteria",
]


def test_schema_property_order_matches_the_models_emission_order():
    """llama.cpp compiles the schema into an ORDER-SENSITIVE grammar.

    Skipping an optional key is legal; coming BACK to it is not. When the model does,
    the only continuation the grammar allows is string content, so its attempt to write
    `","acceptance_criteria":` is escaped INTO the open string as \\" and the string never
    terminates -> "Unterminated string" -> the decompose fails, often through the REPLAN.

    Measured on v224: BOTH failures died at the identical field --
        "output_signature":"float\\",\\"acceptance_criteria\\":[...
    acceptance_criteria sat at index 2 while the model reaches for it LAST. 2 of 3
    decomposes lost. Listing the keys in emission order removes the backtrack entirely.

    JSON object key order carries no semantics and the spec builder reads by name, so
    this ordering is free -- but it is NOT cosmetic. Do not "tidy" it.
    """
    from autopilot_graph import _decompose_schema
    props = list(_decompose_schema(require_function_name=True)["properties"]["subtasks"]["items"]["properties"])
    idx = [props.index(k) for k in _MODEL_EMISSION_ORDER]
    assert idx == sorted(idx), (
        f"schema order forces the model to backtrack: {_MODEL_EMISSION_ORDER} maps to {idx}. "
        "The grammar cannot express a backtrack, so the model escapes its way into an "
        "unterminated string. Keep schema order == emission order."
    )


def test_acceptance_criteria_comes_after_the_interface_contract():
    """The specific regression: acceptance_criteria at index 2 vs the model wanting it last."""
    from autopilot_graph import _decompose_schema
    props = list(_decompose_schema(require_function_name=True)["properties"]["subtasks"]["items"]["properties"])
    assert props.index("acceptance_criteria") > props.index("output_signature")


def test_reorder_changed_nothing_but_order():
    """Guards against the reorder having quietly dropped or altered a field.

    2026-07-23: `deliverable` added ON PURPOSE (see tests/test_deliverable_kind.py). This
    guard caught it, which is the guard working -- the set is updated deliberately, not
    loosened. It stays an exact-set assertion so the next unintended field still trips it.
    """
    from autopilot_graph import _decompose_schema, MAX_SUBTASKS
    s = _decompose_schema(require_function_name=True)
    item = s["properties"]["subtasks"]["items"]
    assert set(item["properties"]) == {
        "subtask_id", "description", "deliverable", "acceptance_criteria", "assigned_tier",
        "complexity_band", "dependencies", "think",
        "function_name", "input_signature", "output_signature",
    }
    assert item["required"] == [
        "subtask_id", "description", "deliverable", "assigned_tier", "function_name",
    ]
    assert item["properties"]["assigned_tier"]["enum"] == ["1b", "1a-hearth", "2_devstral", "2_qwen_coder"]
    assert s["properties"]["subtasks"]["minItems"] == 2
    assert s["properties"]["subtasks"]["maxItems"] == MAX_SUBTASKS
