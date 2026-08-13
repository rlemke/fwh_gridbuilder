# SPDX-License-Identifier: Apache-2.0
"""The retrieval library and its reuse decision.

Runs fully offline (`use_mock=True`) — no network, no MongoDB — because the
thing under test is the ORCHESTRATION contract, not OpenStreetMap's current
contents. Tests that assert on live OSM data would fail on a Tuesday when
somebody remaps a substation.

The reuse tests are the ones that matter. They pin the difference this package
exists to demonstrate: upstream's `rule retrieve_osm` declares no file inputs,
so once its outputs exist nothing can make them look stale and re-running serves
whatever was downloaded the first time. Here every condition is about identity.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "gridbuilder" / "tools"))

from _gridbuilder_tools import retrieve, validate  # noqa: E402


@pytest.fixture
def extract(tmp_path):
    """One BE/substation extraction in a fresh directory."""
    return retrieve.retrieve_feature("BE", "substation", str(tmp_path), use_mock=True)


# ---------------------------------------------------------------------------
# Output naming — the file-for-file comparison with upstream depends on this
# ---------------------------------------------------------------------------


def test_output_names_match_upstream_pattern():
    """`{country}_{feature}.csv` is `retrieve.smk`'s output pattern verbatim.
    Diverge here and the two projects' runs can only be described, not diffed."""
    assert retrieve.output_names("BE", "substation") == (
        "BE_substation.csv",
        "BE_substation.geojson",
    )


def test_retrieve_writes_both_formats(extract, tmp_path):
    assert Path(extract.csv_path).is_file()
    assert Path(extract.geojson_path).is_file()
    assert extract.elements == 3
    assert extract.was_cached is False
    geo = json.loads(Path(extract.geojson_path).read_text())
    assert len(geo["features"]) == 3


def test_element_count_excludes_the_header(tmp_path):
    """The number downstream cares about. Counting the header would make an
    empty extract look like it has one element — exactly the failure the
    validate stage exists to catch."""
    p = tmp_path / "empty.csv"
    p.write_text("id,power,voltage,tags\n")
    assert retrieve.count_rows(str(p)) == 0


def test_counting_a_missing_file_is_zero_not_an_error(tmp_path):
    assert retrieve.count_rows(str(tmp_path / "nope.csv")) == 0


# ---------------------------------------------------------------------------
# The reuse decision
# ---------------------------------------------------------------------------


def test_a_second_run_reuses_the_extract(extract, tmp_path):
    again = retrieve.retrieve_feature("BE", "substation", str(tmp_path), use_mock=True)
    assert again.was_cached is True
    assert again.elements == extract.elements


def test_force_re_extracts(extract, tmp_path):
    again = retrieve.retrieve_feature("BE", "substation", str(tmp_path), use_mock=True, force=True)
    assert again.was_cached is False


def test_a_missing_sidecar_means_re_extract(extract, tmp_path):
    """Provenance is what licenses reuse. Without it the artifact is just bytes
    of unknown origin, and the safe reading of unknown is 'stale'."""
    Path(retrieve.sidecar_path(extract.csv_path)).unlink()
    again = retrieve.retrieve_feature("BE", "substation", str(tmp_path), use_mock=True)
    assert again.was_cached is False


def test_a_truncated_extract_is_not_reused(extract, tmp_path):
    """A half-written CSV is the same size check `fw.http.Fetch` makes: an
    interrupted write must never be served as a complete download."""
    Path(extract.csv_path).write_text("id,power\n")
    again = retrieve.retrieve_feature("BE", "substation", str(tmp_path), use_mock=True)
    assert again.was_cached is False


def test_a_new_extractor_version_invalidates(extract, tmp_path, monkeypatch):
    """THE case Snakemake's mtime cannot express, and the reason this package
    records a version at all: the data on disk is untouched and perfectly
    fresh-looking, but the code that produced it has changed underneath."""
    side_path = Path(retrieve.sidecar_path(extract.csv_path))
    side = json.loads(side_path.read_text())
    side["extractor_version"] = "0.0.1-ancient"
    side_path.write_text(json.dumps(side))

    ok, why = retrieve.is_current(
        extract.csv_path,
        country="BE",
        feature="substation",
        primary_name="power",
        source="geofabrik",
    )
    assert ok is False
    assert "earth-osm" in why


def test_a_config_change_invalidates(extract):
    """Same output path, different question asked. Reusing here would serve the
    previous config's data under the new config's name."""
    ok, why = retrieve.is_current(
        extract.csv_path,
        country="BE",
        feature="substation",
        primary_name="power",
        source="overpass",  # was geofabrik
    )
    assert ok is False and "source" in why


def test_a_corrupt_sidecar_does_not_crash_the_run(extract, tmp_path):
    Path(retrieve.sidecar_path(extract.csv_path)).write_text("{ not json")
    again = retrieve.retrieve_feature("BE", "substation", str(tmp_path), use_mock=True)
    assert again.was_cached is False


def test_an_unknown_source_is_rejected_before_any_work(tmp_path):
    with pytest.raises(ValueError, match="source must be one of"):
        retrieve.retrieve_feature("BE", "substation", str(tmp_path), source="carrier-pigeon")


# ---------------------------------------------------------------------------
# Validation (beyond upstream parity)
# ---------------------------------------------------------------------------


def test_validate_accepts_a_real_extract(extract):
    verdict = validate.validate_extract(extract.csv_path)
    assert verdict.ok is True and verdict.elements == 3


def test_validate_rejects_an_empty_extract_with_an_actionable_message(tmp_path):
    """An empty extract is a successful download of nothing. Downstream it
    becomes a grid with no substations, which reads as a modelling result."""
    p = tmp_path / "BE_substation.csv"
    p.write_text("id,power,voltage,tags\n")
    verdict = validate.validate_extract(str(p))
    assert verdict.ok is False and verdict.elements == 0
    assert "region code" in verdict.detail


def test_validate_honours_a_higher_threshold(extract):
    assert validate.validate_extract(extract.csv_path, min_elements=99).ok is False
