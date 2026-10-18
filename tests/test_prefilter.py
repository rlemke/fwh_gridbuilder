# SPDX-License-Identifier: Apache-2.0
"""Bring-your-own PBF: the power prefilter and the seeded earth_osm run.

The osmium-backed tests build a tiny PBF from OSM XML, so they assert on a
fixture rather than on live OpenStreetMap, and skip where the osmium CLI is not
installed. The end-to-end test points every proxy at a dead port: a seeded
extraction that still reached for Geofabrik fails it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "gridbuilder" / "tools"))

from _gridbuilder_tools import prefilter, retrieve  # noqa: E402

needs_osmium = pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium CLI not installed")

# A 220 kV line between two towers, a substation area, and a road that must be
# filtered away together with its nodes.
_OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="49.60" lon="6.10"><tag k="power" v="tower"/></node>
  <node id="2" version="1" lat="49.61" lon="6.12"><tag k="power" v="tower"/></node>
  <node id="3" version="1" lat="49.62" lon="6.13"/>
  <node id="4" version="1" lat="49.62" lon="6.14"/>
  <node id="5" version="1" lat="49.63" lon="6.14"/>
  <node id="6" version="1" lat="49.70" lon="6.20"/>
  <node id="7" version="1" lat="49.71" lon="6.21"/>
  <way id="10" version="1"><nd ref="1"/><nd ref="2"/>
    <tag k="power" v="line"/><tag k="voltage" v="220000"/></way>
  <way id="11" version="1"><nd ref="3"/><nd ref="4"/><nd ref="5"/><nd ref="3"/>
    <tag k="power" v="substation"/><tag k="voltage" v="220000"/></way>
  <way id="12" version="1"><nd ref="6"/><nd ref="7"/><tag k="highway" v="residential"/></way>
</osm>
"""


@pytest.fixture
def tiny_pbf(tmp_path):
    xml = tmp_path / "tiny.osm"
    xml.write_text(_OSM_XML)
    pbf = tmp_path / "src" / "tiny-latest.osm.pbf"
    pbf.parent.mkdir()
    subprocess.run(["osmium", "cat", "-o", str(pbf), str(xml)], check=True)
    return pbf


def _ids(pbf: Path) -> set[str]:
    out = subprocess.run(["osmium", "cat", "-f", "opl", str(pbf)], capture_output=True, text=True, check=True)
    return {line.split()[0] for line in out.stdout.splitlines()}


def test_filter_expression_is_the_whole_key():
    assert prefilter.filter_expression("power") == "nwr/power"
    for bad in ("", "power=line", "w/power"):
        with pytest.raises(ValueError):
            prefilter.filter_expression(bad)


def test_filtered_name_carries_source_identity():
    a = prefilter.filtered_name("s3://b/europe-latest.osm.pbf", "power", "size=1;etag=x")
    b = prefilter.filtered_name("s3://b/europe-latest.osm.pbf", "power", "size=1;etag=y")
    assert a.startswith("europe-latest.power.") and a.endswith(".osm.pbf")
    assert a != b  # a refreshed source can never match the old filtered file


@needs_osmium
def test_prefilter_keeps_power_and_its_nodes_only(tiny_pbf, tmp_path):
    got = prefilter.prefilter(str(tiny_pbf), str(tmp_path / "flt"))
    assert not got.was_cached
    ids = _ids(Path(got.filtered))
    assert {"w10", "w11", "n1", "n2", "n3", "n4", "n5"} <= ids  # untagged n3-n5: referenced
    assert not {"w12", "n6", "n7"} & ids
    assert Path(got.filtered + ".meta.json").exists()


@needs_osmium
def test_prefilter_reuses_then_refilters_when_source_moves(tiny_pbf, tmp_path):
    out = str(tmp_path / "flt")
    first = prefilter.prefilter(str(tiny_pbf), out)
    again = prefilter.prefilter(str(tiny_pbf), out)
    assert again.was_cached and again.filtered == first.filtered
    os.utime(tiny_pbf, (1, 1))  # identity is size+mtime for a local source
    moved = prefilter.prefilter(str(tiny_pbf), out)
    assert not moved.was_cached and moved.filtered != first.filtered


@needs_osmium
def test_seeded_extraction_never_touches_the_network(tiny_pbf, tmp_path, monkeypatch):
    pytest.importorskip("earth_osm")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    flt = prefilter.prefilter(str(tiny_pbf), str(tmp_path / "flt"))
    out = str(tmp_path / "out")
    got = retrieve.retrieve_feature("LU", "line", out, pbf=flt.filtered, mp=False)
    assert got.elements == 1 and not got.was_cached
    side = retrieve.read_sidecar(got.csv_path)
    assert side["source"] == "pbf" and side["pbf"] == flt.filtered and side["pbf_identity"]
    # Same PBF → reused; a different one is a different input.
    assert retrieve.retrieve_feature("LU", "line", out, pbf=flt.filtered, mp=False).was_cached
    other = tmp_path / "other.osm.pbf"
    shutil.copyfile(flt.filtered, other)
    assert not retrieve.retrieve_feature("LU", "line", out, pbf=str(other), mp=False).was_cached


def test_pbf_source_requires_a_pbf(tmp_path):
    with pytest.raises(ValueError, match="needs a pbf"):
        retrieve.retrieve_feature("LU", "line", str(tmp_path), source="pbf")
