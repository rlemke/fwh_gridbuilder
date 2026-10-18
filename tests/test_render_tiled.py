# SPDX-License-Identifier: Apache-2.0
"""Vector-tile rendering for continent/world grids."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "gridbuilder" / "tools"))

from _gridbuilder_tools import render_tiled as rt  # noqa: E402


def test_backbone_appears_before_distribution():
    assert rt.minzoom_for("line", 380) == 0
    assert rt.minzoom_for("line", 110) < rt.minzoom_for("line", 33) < rt.minzoom_for("line", 11)
    assert rt.minzoom_for("line", None) == rt.minzoom_for("line", 0)  # unknown = low
    # Every feature must survive the shallowest retile.
    for layer in rt.LAYERS:
        for kv in (None, 0, 11, 33, 110, 220, 380):
            assert rt.minzoom_for(layer, kv) <= rt.MIN_MAXZOOM


def test_slim_feature_drops_refs_and_converts_float_voltage():
    f = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
         "properties": {"id": 7, "refs": list(range(500)), "tags.voltage": 69000.0, "tags.name": "A"}}
    s = rt.slim_feature("cable", f)
    assert s["properties"] == {"id": 7, "kv": 69, "name": "A", "voltage": 69000.0}
    assert s["tippecanoe"]["minzoom"] == rt.minzoom_for("cable", 69)
    assert rt.slim_feature("line", {"properties": {}}) is None


def test_layer_of_uses_the_upstream_suffix():
    assert rt.layer_of("s3://b/x/europe_line.geojson") == "line"
    assert rt.layer_of("LU_substation.geojson") == "substation"
    assert rt.layer_of("LU_generator.geojson") is None


@pytest.mark.skipif(shutil.which("tippecanoe") is None, reason="tippecanoe not installed")
def test_tile_region_and_page(tmp_path):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[6.1, 49.6], [6.2, 49.7]]},
         "properties": {"id": 1, "tags.voltage": "220000"}}]}
    src = tmp_path / "LU_line.geojson"
    src.write_text(json.dumps(fc))
    got = rt.tile_region("LU", [str(src)], str(tmp_path / "site" / "LU.pmtiles"), maxzoom=8)
    assert got.features == {"line": 1} and got.bytes > 0
    meta = subprocess.run(["tippecanoe-decode", "--stats", got.pmtiles], capture_output=True, text=True)
    assert str(tmp_path) not in meta.stdout  # no local path in published metadata
    page = rt.build_page([got.as_dict()], str(tmp_path / "site" / "index.html"), title="LU <grid>")
    html = Path(page).read_text()
    assert '"url": "LU.pmtiles"' in html and "LU &lt;grid&gt;" in html
    assert Path(page).stat().st_mode & 0o044  # readable when served
