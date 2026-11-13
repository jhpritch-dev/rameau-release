"""The routing cache as the ROUTER IMAGE will have it (2026-11-13).

WHICH COPY THIS TESTS, and why that question needs asking. `routing_cache.py`,
`routing_cache_serve_path.py` and `shared/resolver.py` now exist TWICE in this repository:
the published source (`harness/router/`, `shared/`) and the copy inside the router's Docker
build context (`services/router/`). The repository's root `conftest.py` puts `harness/router`
at the FRONT of `sys.path`, so a bare `import routing_cache` anywhere in this suite resolves
to the HARNESS copy -- including from this file. A test that believed otherwise would report
on the published source while claiming to cover the image.

So this file does two separate things and does not confuse them:

  * `test_shipped_copies_are_byte_identical` pins the copies together. That is what lets the
    existing harness tests (test_routing_cache_serve.py, test_routing_cache_serve_path.py,
    test_routing_cache_perf.py) count as coverage of the shipped modules at all. It is a
    CONTENT check, not a name check -- two files with the same name and different bytes is the
    exact failure it exists to catch.

  * everything else runs in a SUBPROCESS whose sys.path is `services/router` alone. That is
    the image's layout (`WORKDIR /app`, sources at the root, `shared/` beside them), so an
    import that only works because the repository root happens to be importable fails here
    the way it would fail in the container -- which is the failure mode that matters, because
    `ModuleNotFoundError: shared` happens at router import time and no /health check can see
    a container that never started.
"""
import filecmp
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SHIPPED = ROOT / "services" / "router"

# (published, shipped). Every file duplicated into a service build context belongs here; the
# shell-MCP pair is included because it has the same divergence exposure and had no check at
# all before this file existed.
COPIES = [
    ("harness/router/routing_cache.py", "services/router/routing_cache.py"),
    ("harness/router/routing_cache_serve_path.py", "services/router/routing_cache_serve_path.py"),
    ("shared/resolver.py", "services/router/shared/resolver.py"),
    ("harness/router/checks.py", "services/shell-mcp/checks.py"),
    ("harness/router/bifrost_shell_static.py", "services/shell-mcp/bifrost_shell_static.py"),
]


@pytest.mark.parametrize("published,shipped", COPIES, ids=[c[1] for c in COPIES])
def test_shipped_copies_are_byte_identical(published, shipped):
    """A service's copy must equal the published source byte for byte.

    shallow=False forces a content comparison. The default compares os.stat signatures, which
    on a fresh checkout can match for files that differ and differ for files that match -- it
    would make this assertion depend on checkout order rather than on content.
    """
    a, b = ROOT / published, ROOT / shipped
    assert a.exists(), "published source missing: %s" % published
    assert b.exists(), "shipped copy missing: %s" % shipped
    assert filecmp.cmp(a, b, shallow=False), (
        "%s has diverged from %s. These are copies on purpose; edit the published file and "
        "re-copy, do not patch the service's copy to make a build fit." % (shipped, published)
    )


def _in_image_layout(snippet, env_extra=None):
    """Run `snippet` with sys.path = [services/router] only. Returns parsed JSON from stdout.

    -I is deliberate: isolated mode drops the CWD and user site-packages, so nothing from the
    repository root leaks onto the path and makes an import look available when the container
    would not have it. The path is then set explicitly inside the snippet.
    """
    env = dict(os.environ)
    # PYTHONPATH would be ignored under -I, so the snippet sets sys.path itself. Clear the
    # cache's own switches so an operator's shell cannot change what this test measures.
    for var in ("BIFROST_ROUTING_CACHE_SERVE", "BIFROST_ROUTING_CACHE_SERVE_BANDS",
                "RAMEAU_EMBED_URL", "BIFROST_ROUTING_CACHE_EMBED_URL",
                "HEARTH_VEGA8_URL", "BIFROST_AUTOPILOT_EMBED_URL"):
        env.pop(var, None)
    env.update(env_extra or {})
    prelude = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n" % str(SHIPPED)
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", prelude + snippet],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, (
        "snippet failed in the image's path layout\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (proc.stdout, proc.stderr)
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_shipped_modules_import_with_only_the_image_on_the_path():
    """The import closure is complete inside the build context.

    This is the check that would have caught a missing `shared/` in the Dockerfile context:
    the router raises on `from shared.resolver import resolve` at import time, so the
    container exits before serving and there is no endpoint left to probe.

    It also pins WHICH files were imported, so a pass cannot come from the repository root
    being importable after all.
    """
    out = _in_image_layout(
        "import routing_cache, routing_cache_serve_path, routing_cache_wiring\n"
        "import shared.resolver\n"
        "print(json.dumps({k: v.__file__ for k, v in {\n"
        "    'routing_cache': routing_cache,\n"
        "    'serve_path': routing_cache_serve_path,\n"
        "    'wiring': routing_cache_wiring,\n"
        "    'resolver': shared.resolver,\n"
        "}.items()}))\n"
    )
    for name, path in out.items():
        assert pathlib.Path(path).resolve().is_relative_to(SHIPPED), (
            "%s imported from %s, which is outside the build context" % (name, path)
        )


def test_the_cache_is_off_unless_the_operator_turns_it_on():
    """The shipped default. `build_cache` returns None, so nothing is constructed and the
    persisted file -- which `load_from_disk` feeds to `pickle.load` -- is never read."""
    out = _in_image_layout(
        "import routing_cache, routing_cache_wiring as w\n"
        "print(json.dumps({'enabled': routing_cache.serve_enabled(),\n"
        "                  'built': w.build_cache() is not None}))\n"
    )
    assert out == {"enabled": False, "built": False}


def test_an_unconfigured_embedder_raises_rather_than_guessing():
    """No literal fallback. The published source defaults this to a private LAN address; the
    release resolves through the registry, which raises when nothing is set."""
    out = _in_image_layout(
        "import routing_cache_wiring as w\n"
        "from shared.resolver import ConfigError\n"
        "try:\n"
        "    url = w.embed_url()\n"
        "    print(json.dumps({'raised': False, 'url': url}))\n"
        "except ConfigError as e:\n"
        "    print(json.dumps({'raised': True, 'mentions_var': 'RAMEAU_EMBED_URL' in str(e)}))\n"
    )
    assert out["raised"] is True, "embed_url returned %r with nothing configured" % out.get("url")
    assert out["mentions_var"] is True, "the error must name the variable to set"


@pytest.mark.parametrize("configured,expected", [
    # An ORIGIN, the shape shared/resolver.py documents as this endpoint's example: the
    # Ollama route is appended.
    ("http://embed.example:11436", "http://embed.example:11436/api/embeddings"),
    # A bare trailing slash is an origin too -- resolve() strips it, measured 2026-11-13.
    ("http://embed.example:11436/", "http://embed.example:11436/api/embeddings"),
    # A COMPLETE target, the shape the published source's own getenv expects: used as given.
    # Appending here would produce /api/embeddings/api/embeddings and a 404 that reads as a
    # broken embedder rather than as a doubled path.
    ("http://embed.example:11436/api/embeddings",
     "http://embed.example:11436/api/embeddings"),
    # Any other path is also the caller meaning it -- a reverse proxy in front of the embedder
    # is the ordinary reason to have one.
    ("http://gw.example/embed/v1", "http://gw.example/embed/v1"),
])
def test_both_documented_endpoint_shapes_resolve_to_one_target(configured, expected):
    out = _in_image_layout(
        "import routing_cache_wiring as w\n"
        "print(json.dumps({'url': w.embed_url()}))\n",
        env_extra={"RAMEAU_EMBED_URL": configured},
    )
    assert out["url"] == expected


def test_the_compatibility_alias_still_configures_this_call_site():
    """`BIFROST_ROUTING_CACHE_EMBED_URL` is the name the published source reads. The registry
    carries it as an alias, so an operator who has not migrated keeps working -- and one who
    sets only RAMEAU_EMBED_URL configures this along with every other embed consumer, which
    is the thing the registry exists to make true."""
    out = _in_image_layout(
        "import routing_cache_wiring as w\n"
        "print(json.dumps({'url': w.embed_url()}))\n",
        env_extra={"BIFROST_ROUTING_CACHE_EMBED_URL": "http://legacy.example:11436"},
    )
    assert out["url"] == "http://legacy.example:11436/api/embeddings"


def test_the_endpoint_names_are_the_registrys_and_not_a_second_list():
    """The wiring must not grow its own copy of the variable names.

    A hand-copied list is the divergence this asserts against: edit the registry's aliases and
    this fails, rather than leaving two lists that agree today. Imported from the REPOSITORY
    here (conftest makes `shared` importable), which is the authority; the image's copy is
    pinned to it by the byte-identity test above.
    """
    sys.path.insert(0, str(ROOT))
    from shared.resolver import ENDPOINTS

    embed = ENDPOINTS["embed"]
    out = _in_image_layout(
        "import routing_cache_wiring as w\n"
        "names = []\n"
        "for n in %r:\n"
        "    try:\n"
        "        names.append((n, w.embed_url({n: 'http://x.example:1/'})))\n"
        "    except Exception as e:\n"
        "        names.append((n, 'RAISED:' + type(e).__name__))\n"
        "print(json.dumps(dict(names)))\n" % ((embed.env,) + tuple(embed.aliases),)
    )
    for name, url in out.items():
        assert url == "http://x.example:1/api/embeddings", (
            "%s is in the registry for 'embed' but does not configure the routing cache "
            "(got %r)" % (name, url)
        )


CASCADE_DOC = SHIPPED / "skills" / "router" / "references" / "cascade-tables.md"


def _md_tables(text):
    """Markdown tables grouped by the heading above them: {heading: [[cell, ...], ...]}.

    STRUCTURAL ON PURPOSE. The obvious way to check this document is to grep it for the stale
    tier names, and that check cannot work here: the file ends with a section that NAMES every
    retired claim deliberately, so a name count cannot tell a live table cell from a historical
    note and would fail on the retirement it is supposed to reward. Parsing tables ignores
    prose entirely, which is the distinction that matters.
    """
    out, heading = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or all(set(c) <= set("-: ") for c in cells):
            continue                      # separator row
        out.setdefault(heading, []).append(cells)
    return out


def _unbacktick(s):
    return s.replace("`", "").strip()


def test_the_cascade_document_matches_the_shipped_config():
    """The shipped skill reference must agree with the config.py beside it.

    This document described a different deployment until 2026-11-13: two tiers that do not
    exist, an operating mode that does not exist, three wrong models, and every tier written
    as its enum MEMBER name rather than the VALUE the API accepts -- so a reader following it
    to set X-Tier-Override would have sent something unroutable. Nothing could have caught
    that, because nothing compared the two.

    Set equality in BOTH directions, deliberately: a missing row and an invented row are
    different defects and a one-way check sees only one of them.
    """
    tables = _md_tables(CASCADE_DOC.read_text(encoding="utf-8"))

    truth = _in_image_layout(
        "from config import CASCADE_TABLES, TIER_BACKENDS, ComplexityBand\n"
        "print(json.dumps({\n"
        "  'tiers': {t.value: (b.get('type'), b.get('model') or b.get('provider'))\n"
        "            for t, b in TIER_BACKENDS.items()},\n"
        "  'bands': [b.value for b in ComplexityBand],\n"
        "  'cascades': {m.value: {b.value: [t.value for t in v] for b, v in tbl.items()}\n"
        "               for m, tbl in CASCADE_TABLES.items()},\n"
        "}))\n"
    )

    # --- the tier map ---------------------------------------------------------------
    tier_rows = tables.get("Tier → backend map")
    assert tier_rows, "no tier table found under its heading; the document was restructured"
    documented = {_unbacktick(r[0]): _unbacktick(r[2]) for r in tier_rows[1:]}
    assert documented == {k: v[1] for k, v in truth["tiers"].items()}, (
        "the tier map disagrees with TIER_BACKENDS.\ndocumented: %s\nconfig.py:  %s"
        % (sorted(documented.items()), sorted((k, v[1]) for k, v in truth["tiers"].items()))
    )

    # --- the cascades, one table per mode -------------------------------------------
    for mode, bands in truth["cascades"].items():
        rows = tables.get(mode)
        assert rows, (
            "config.py defines operating mode %r and the document has no table for it" % mode
        )
        doc_cascade = {
            r[0].strip(): [_unbacktick(t) for t in r[1].split("→")]
            for r in rows[1:]
        }
        assert doc_cascade == bands, (
            "mode %s disagrees.\ndocumented: %s\nconfig.py:  %s"
            % (mode, sorted(doc_cascade.items()), sorted(bands.items()))
        )

    # --- and no table for a mode that does not exist --------------------------------
    # The JARVIS_OFFLINE case: the document carried a full four-row cascade table for a mode
    # config.py has never had. The loop above cannot see that -- it iterates over what EXISTS.
    documented_modes = {
        h for h, rows in tables.items()
        if h and rows and rows[0][0].strip().lower() == "band"
    }
    assert documented_modes <= set(truth["cascades"]), (
        "the document has a cascade table for %s, which config.py does not define"
        % sorted(documented_modes - set(truth["cascades"]))
    )


def test_a_cache_served_result_is_never_written_back():
    """Fed the telemetry dict the serve path actually builds, not a hand-written one.

    The whole mechanism is one key spelling shared between two modules; a test that spelled it
    itself would pass while the real pair disagreed.
    """
    out = _in_image_layout(
        "import routing_cache, routing_cache_serve_path as sp, routing_cache_wiring as w\n"
        "served = sp.routing_td(routing_cache.ServeDecision(\n"
        "    serve=True, reason='hit', band='MODERATE', confidence=0.9, similarity=0.97))\n"
        "classified = {'routing_source': 'classifier'}\n"
        "print(json.dumps({\n"
        "    'served': w.should_write_back(served, None),\n"
        "    'classified': w.should_write_back(classified, None),\n"
        "    'classified_but_hinted': w.should_write_back(classified, 'COMPLEX'),\n"
        "    'source_key_present': 'routing_source' in served,\n"
        "}))\n"
    )
    assert out["source_key_present"] is True, (
        "routing_td() no longer carries 'routing_source'; should_write_back cannot see the "
        "difference between a served and a classified result"
    )
    assert out["served"] is False, "a cache-served result would be written back to the cache"
    assert out["classified"] is True, "a genuine classification must be storable, or the "\
        "cache can never hold anything and every request is a miss"
    assert out["classified_but_hinted"] is False, (
        "a hinted request must not be stored -- the serve path refuses to ANSWER one, and "
        "storing it would let one caller's override reach later requests"
    )
