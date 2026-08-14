# SPDX-License-Identifier: Apache-2.0
"""Handlers, and the FFL that declares them.

The FFL test is the one that earns its place: a facet declared in `.ffl` with no
handler behind it compiles, dispatches, and then dies on a runner hours later
with "no handler for facet" — far from the change that caused it. Comparing the
two sides here turns that into a failing test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from gridbuilder.handlers.audit import audit_handlers  # noqa: E402
from gridbuilder.handlers.render import render_handlers  # noqa: E402
from gridbuilder.handlers.retrieve import retrieve_handlers  # noqa: E402
from gridbuilder.handlers.validate import validate_handlers  # noqa: E402

FFL_PATH = _SRC / "gridbuilder" / "ffl" / "gridbuilder.ffl"
AUDIT_FFL = _SRC / "gridbuilder" / "ffl" / "audit.ffl"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def test_retrieve_handler_returns_the_declared_schema(tmp_path):
    out = retrieve_handlers.handle(
        {
            "_facet_name": "grid.osm.Retrieve",
            "country": "BE",
            "feature": "substation",
            "out_dir": str(tmp_path),
            "use_mock": True,
        }
    )
    assert set(out) == {"result"}
    result = out["result"]
    # Field-for-field with `schema FeatureSet` in the FFL.
    assert set(result) == {
        "country",
        "feature",
        "csv_path",
        "geojson_path",
        "elements",
        "was_cached",
    }
    assert result["elements"] == 3 and result["was_cached"] is False


def test_retrieve_handler_reports_reuse_on_the_second_call(tmp_path):
    payload = {
        "_facet_name": "grid.osm.Retrieve",
        "country": "BE",
        "feature": "line",
        "out_dir": str(tmp_path),
        "use_mock": True,
    }
    retrieve_handlers.handle(payload)
    assert retrieve_handlers.handle(payload)["result"]["was_cached"] is True


def test_validate_handler(tmp_path):
    csv_path = tmp_path / "BE_substation.csv"
    csv_path.write_text("id,power\n1,substation\n")
    out = validate_handlers.handle(
        {"_facet_name": "grid.osm.ValidateRetrieval", "csv_path": str(csv_path)}
    )
    assert out["ok"] is True and out["elements"] == 1


@pytest.mark.parametrize(
    "module", [retrieve_handlers, validate_handlers, render_handlers, audit_handlers]
)
def test_unknown_facet_is_a_clear_error(module):
    with pytest.raises(KeyError):
        module.handle({"_facet_name": "grid.osm.NotAFacet"})


# ---------------------------------------------------------------------------
# The FFL and the handlers must not drift
# ---------------------------------------------------------------------------


def test_every_declared_event_facet_has_a_handler():
    declared = {
        f"grid.osm.{m}"
        for m in re.findall(r"event facet\s+(\w+)\s*\(", FFL_PATH.read_text())
    } | {
        f"grid.audit.{m}"
        for m in re.findall(r"event facet\s+(\w+)\s*\(", AUDIT_FFL.read_text())
    }
    handled = (
        set(retrieve_handlers.facet_names())
        | set(validate_handlers.facet_names())
        | set(render_handlers.facet_names())
        | set(audit_handlers.facet_names())
    )

    assert declared, "no event facets parsed from the FFL"
    assert declared == handled, (
        f"declared without a handler: {sorted(declared - handled)}; "
        f"handled but not declared: {sorted(handled - declared)}"
    )


def test_the_audit_ffl_compiles():
    from facetwork import parse
    from facetwork.validator import validate as validate_ffl

    result = validate_ffl(parse(AUDIT_FFL.read_text()))
    assert result.is_valid, "; ".join(e.message for e in result.errors)


def test_the_ffl_compiles():
    """Compiled with the real compiler, not regex-checked. The built-in `fw.*`
    facets it uses need no --library: the framework knows its own."""
    from facetwork import parse
    from facetwork.validator import validate as validate_ffl

    result = validate_ffl(parse(FFL_PATH.read_text()))
    assert result.is_valid, "; ".join(e.message for e in result.errors)


def test_the_domain_package_is_discoverable():
    """What `fw runner start --domain gridbuilder` and `fw ffl seed` load."""
    from gridbuilder import domain

    assert domain.name == "gridbuilder"
    assert (domain.ffl_dir / "gridbuilder.ffl").is_file()


# ---------------------------------------------------------------------------
# The documented examples must keep compiling
# ---------------------------------------------------------------------------


def test_every_ffl_block_in_the_examples_doc_compiles():
    """docs/ffl-examples.md claims its blocks compile. This is that claim.

    Without it the gallery rots the first time a facet signature changes, and
    the failure surfaces as a copied snippet that does not compile for whoever
    trusted the doc.
    """
    import re

    from facetwork import parse
    from facetwork.validator import validate as validate_ffl

    doc = (Path(__file__).resolve().parents[1] / "docs" / "ffl-examples.md").read_text()
    blocks = re.findall(r"```ffl\n(.*?)```", doc, re.S)
    assert blocks, "no ffl blocks found — did the doc move?"

    # Compiled in-process against this domain's own FFL: the blocks `use
    # grid.osm`, so the declarations have to be visible to the validator.
    library = FFL_PATH.read_text()
    failures = []
    for i, block in enumerate(blocks, 1):
        result = validate_ffl(parse(library + "\n" + block))
        if not result.is_valid:
            failures.append(f"block {i}: " + "; ".join(e.message for e in result.errors[:2]))
    assert not failures, "\n".join(failures)


def test_fanout_yields_wrap_their_value_in_a_list():
    """Guards the mistake this package was built with and had to fix.

    `yield F(xs = item)` ASSIGNS, so each iteration overwrites the last and a
    fan-out returns only its final iteration — with every iteration's step
    still running and the workflow still reporting success. `yield F(xs =
    [item])` concatenates. Verified on a runner: 2 countries x 2 features gave
    1 manifest entry with the bare form and 4 with the list form.
    """
    import re

    source = FFL_PATH.read_text()
    bare = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"yield \w+\(\w+ = (?!\[)[\w.]+\)", line.strip())
        and "countries_done" not in line
    ]
    assert not bare, (
        "fan-out yield without brackets — the result will be the last iteration only:\n  "
        + "\n  ".join(bare)
    )
