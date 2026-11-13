from pathlib import Path
import ast
import pytest
import classifier
from config import ComplexityBand


def test_classifier_pure_ascii():
    """classifier.py must be 100% valid ASCII to prevent Windows cp1252 UnicodeEncodeError."""
    p = Path(classifier.__file__)
    content = p.read_bytes().decode("utf-8")
    non_ascii = [c for c in content if ord(c) > 127]
    assert len(non_ascii) == 0, f"Found {len(non_ascii)} non-ASCII characters: {[hex(ord(c)) for c in set(non_ascii)]}"


def test_classifier_cp1252_encoding():
    """classifier.py file content must be directly encodable to cp1252 without error."""
    p = Path(classifier.__file__)
    content = p.read_bytes().decode("utf-8")
    content.encode("cp1252")


def test_no_mid_file_imports():
    """All imports in classifier.py must be at module top (no E402 violations)."""
    p = Path(classifier.__file__)
    tree = ast.parse(p.read_text(encoding="utf-8"))
    
    seen_definition = False
    mid_file_imports = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen_definition = True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if seen_definition:
                mid_file_imports.append(node.lineno)
    
    assert len(mid_file_imports) == 0, f"Found mid-file imports after class/function definitions at lines: {mid_file_imports}"


def test_classifier_functionality():
    """Verify heuristic classification logic operates identically."""
    res_empty = classifier.classify("")
    assert res_empty.band == ComplexityBand.TRIVIAL
    assert isinstance(res_empty.reasoning, str)
    res_empty.reasoning.encode("cp1252")

    res_short = classifier.classify("hi")
    assert res_short.band == ComplexityBand.TRIVIAL
    assert "-> likely autocomplete" in res_short.reasoning
    assert "\u2192" not in res_short.reasoning
    res_short.reasoning.encode("cp1252")

    res_code = classifier.classify("def foo():\n    return 42\n")
    assert isinstance(res_code.band, ComplexityBand)
    assert isinstance(res_code.reasoning, str)
    res_code.reasoning.encode("cp1252")
