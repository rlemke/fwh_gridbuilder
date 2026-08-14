# SPDX-License-Identifier: Apache-2.0
"""The grid quality audit, on synthetic grids with known answers.

Real OSM data cannot pin this down: its defect count changes whenever somebody
remaps a substation, so a test against it would assert on today's OpenStreetMap
rather than on the analysis. Each fixture below is a small grid built to contain
exactly one problem.

The tolerance is the load-bearing parameter (see the module docstring), so it is
tested directly: the same geometry must read as connected or dangling depending
on it, and that has to be a decision the caller makes rather than a constant
buried in the analysis.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "gridbuilder" / "tools"))
sys.path.insert(0, str(_ROOT / "src"))

from _gridbuilder_tools import audit as A  # noqa: E402

# ~0.01 deg longitude at 50N is ~715 m, so these are comfortably apart.
def line(coords, osm_id="1", voltage="380000"):
    return {
        "type": "Feature",
        "properties": {"id": osm_id, "voltage": voltage, "power": "line"},
        "geometry": {"type": "LineString", "coordinates": coords},
    }


def substation(lon, lat, osm_id="s1"):
    return {
        "type": "Feature",
        "properties": {"id": osm_id, "power": "substation"},
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


def fc(features):
    return {"type": "FeatureCollection", "features": features}


def kinds(result):
    return {f["kind"] for f in result["findings"]}


# ---------------------------------------------------------------------------
# Voltage parsing — OSM writes this several ways
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("380000", 380000),
        ("380000;110000", 380000),   # multi-circuit: the highest is the class
        ("110000;380000", 380000),
        ("400 kV", 400000),          # kV spelled out
        ("", None),
        (None, None),
        ("abc", None),
        ("42", None),                # below any plausible transmission voltage
    ],
)
def test_voltage_parsing(raw, expected):
    assert A.parse_voltage(raw) == expected


# ---------------------------------------------------------------------------
# A clean grid must produce nothing
# ---------------------------------------------------------------------------


def test_a_well_formed_grid_reports_no_findings():
    """The most important case: an audit that always finds something is noise."""
    lines = [
        line([[6.10, 49.60], [6.15, 49.62]], "l1"),
        line([[6.15, 49.62], [6.20, 49.64]], "l2"),
    ]
    subs = [substation(6.10, 49.60, "sA"), substation(6.20, 49.64, "sB")]
    result = A.audit(fc(lines), fc(subs))
    assert result["findings"] == []
    assert result["summary"]["islands"] == 1


# ---------------------------------------------------------------------------
# Individual defects
# ---------------------------------------------------------------------------


def test_a_line_stopping_in_a_field_is_flagged():
    lines = [line([[6.10, 49.60], [6.15, 49.62]], "l1")]
    subs = [substation(6.10, 49.60, "sA")]  # only one end is served
    result = A.audit(fc(lines), fc(subs))
    assert "dangling_line_end" in kinds(result)
    dangling = [f for f in result["findings"] if f["kind"] == "dangling_line_end"]
    assert len(dangling) == 1 and dangling[0]["severity"] == "high"
    assert dangling[0]["refs"] == ["l1"]


def test_a_line_entering_a_substation_polygon_is_connected():
    """A switchyard is an area, and a line reaching its corner is connected —
    treating substations as points would flag every large site."""
    poly = {
        "type": "Feature",
        "properties": {"id": "yard", "power": "substation"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[6.199, 49.639], [6.201, 49.639], [6.201, 49.641],
                             [6.199, 49.641], [6.199, 49.639]]],
        },
    }
    lines = [line([[6.10, 49.60], [6.2005, 49.6405]], "l1")]
    result = A.audit(fc(lines), fc([substation(6.10, 49.60, "sA"), poly]))
    assert "dangling_line_end" not in kinds(result)


def test_missing_voltage_is_flagged():
    lines = [
        line([[6.10, 49.60], [6.15, 49.62]], "l1", voltage=""),
        line([[6.15, 49.62], [6.20, 49.64]], "l2"),
    ]
    subs = [substation(6.10, 49.60), substation(6.20, 49.64)]
    result = A.audit(fc(lines), fc(subs))
    assert "missing_voltage" in kinds(result)
    assert result["summary"]["lines_without_voltage"] == 1


def test_a_voltage_step_outside_a_substation_is_flagged():
    """A transformer lives in a substation. 380 kV meeting 110 kV in open
    country means a missing substation or a wrong tag."""
    lines = [
        line([[6.10, 49.60], [6.15, 49.62]], "hv", voltage="380000"),
        line([[6.15, 49.62], [6.20, 49.64]], "mv", voltage="110000"),
    ]
    subs = [substation(6.10, 49.60), substation(6.20, 49.64)]
    result = A.audit(fc(lines), fc(subs))
    assert "implausible_voltage_transition" in kinds(result)


def test_the_same_voltage_step_INSIDE_a_substation_is_fine():
    """The counter-case, which is what stops this rule being noise: with a
    substation at the junction, the transformer is exactly where it belongs."""
    lines = [
        line([[6.10, 49.60], [6.15, 49.62]], "hv", voltage="380000"),
        line([[6.15, 49.62], [6.20, 49.64]], "mv", voltage="110000"),
    ]
    subs = [substation(6.10, 49.60), substation(6.15, 49.62), substation(6.20, 49.64)]
    result = A.audit(fc(lines), fc(subs))
    assert "implausible_voltage_transition" not in kinds(result)


def test_double_mapped_circuits_are_flagged():
    lines = [
        line([[6.10, 49.60], [6.15, 49.62]], "a"),
        line([[6.10, 49.60], [6.15, 49.62]], "b"),   # same route, mapped twice
    ]
    subs = [substation(6.10, 49.60), substation(6.15, 49.62)]
    result = A.audit(fc(lines), fc(subs))
    assert "duplicate_circuit" in kinds(result)


def test_a_genuinely_different_route_is_not_a_duplicate():
    """Same endpoints, longer path — a real parallel corridor, not a mistake."""
    lines = [
        line([[6.10, 49.60], [6.15, 49.62]], "a"),
        line([[6.10, 49.60], [6.12, 49.70], [6.15, 49.62]], "b"),
    ]
    subs = [substation(6.10, 49.60), substation(6.15, 49.62)]
    result = A.audit(fc(lines), fc(subs))
    assert "duplicate_circuit" not in kinds(result)


def test_an_isolated_pair_of_lines_is_an_island():
    main = [
        line([[6.10, 49.60], [6.15, 49.62]], "m1"),
        line([[6.15, 49.62], [6.20, 49.64]], "m2"),
        line([[6.20, 49.64], [6.25, 49.66]], "m3"),
    ]
    far = [line([[7.50, 50.50], [7.55, 50.52]], "i1")]
    subs = [substation(6.10, 49.60), substation(6.25, 49.66),
            substation(7.50, 50.50), substation(7.55, 50.52)]
    result = A.audit(fc(main + far), fc(subs))
    assert "grid_island" in kinds(result)
    assert result["summary"]["islands"] == 2
    assert result["summary"]["largest_island_lines"] == 3


# ---------------------------------------------------------------------------
# The tolerance is a decision, not a constant
# ---------------------------------------------------------------------------


def test_tolerance_decides_whether_a_gap_is_a_gap():
    """Two lines ~80 m apart: connected at a loose tolerance, dangling at a
    tight one. The caller has to own that, and every finding records it."""
    lines = [
        line([[6.1000, 49.6000], [6.1500, 49.6200]], "l1"),
        line([[6.1510, 49.6200], [6.2000, 49.6400]], "l2"),   # ~72 m gap
    ]
    subs = [substation(6.1000, 49.6000), substation(6.2000, 49.6400)]

    loose = A.audit(fc(lines), fc(subs), tolerance_m=150)
    tight = A.audit(fc(lines), fc(subs), tolerance_m=20)

    assert "dangling_line_end" not in kinds(loose)
    assert "dangling_line_end" in kinds(tight)
    assert tight["summary"]["tolerance_m"] == 20
    assert all(f"{20:.0f} m" in f["detail"] or f["kind"] != "dangling_line_end"
               for f in tight["findings"])


def test_findings_are_ranked_worst_first():
    lines = [
        line([[6.10, 49.60], [6.15, 49.62]], "l1", voltage=""),   # medium
        line([[6.30, 49.80], [6.35, 49.82]], "l2"),               # high: dangling
    ]
    result = A.audit(fc(lines), fc([]))
    severities = [f["severity"] for f in result["findings"]]
    assert severities == sorted(severities, key=lambda s: A.SEVERITY_ORDER[s])
    assert severities[0] == "high"


def test_findings_geojson_is_drawable():
    lines = [line([[6.10, 49.60], [6.15, 49.62]], "l1")]
    result = A.audit(fc(lines), fc([]))
    gj = A.findings_geojson(result["findings"])
    assert gj["type"] == "FeatureCollection" and gj["features"]
    f = gj["features"][0]
    assert f["geometry"]["type"] == "Point"
    assert {"kind", "severity", "detail"} <= set(f["properties"])


def test_an_empty_extract_reports_rather_than_crashing_or_going_quiet():
    """A country with no lines is a retrieval problem, not an audit crash —
    and not a pass either. This test previously asserted `findings == []`,
    which is what let Monaco report a clean grid with nothing in it.
    """
    result = A.audit(fc([]), fc([]))
    assert result["summary"]["lines"] == 0
    assert "no_lines_in_extract" in kinds(result)


def test_a_line_teeing_into_another_mid_span_is_connected():
    """OSM tees branches into the MIDDLE of a way constantly. Judging
    connectivity on endpoints alone reported those as dangling ends and split
    the network into dozens of phantom islands — on Luxembourg it turned one
    grid into 52."""
    trunk = line([[6.10, 49.60], [6.20, 49.60], [6.30, 49.60]], "trunk")
    branch = line([[6.20, 49.60], [6.20, 49.70]], "branch")   # joins mid-span
    subs = [substation(6.10, 49.60), substation(6.30, 49.60), substation(6.20, 49.70)]
    result = A.audit(fc([trunk, branch]), fc(subs))
    assert "dangling_line_end" not in kinds(result)
    assert result["summary"]["islands"] == 1


def test_an_extract_with_no_lines_is_a_finding_not_silence():
    """Found by sweeping eight countries: Monaco returned 0 lines and the audit
    reported 0 findings — indistinguishable from a healthy grid. An empty
    extract is the loudest result available, not the quietest."""
    result = A.audit(fc([]), fc([substation(7.42, 43.73)]))
    assert "no_lines_in_extract" in kinds(result)
    assert result["findings"][0]["severity"] == "high"
    assert "power=cable" in result["findings"][0]["detail"], "must name the usual cause"


def test_lines_without_any_substation_says_the_audit_is_unreliable():
    """Every line reads as dangling, so the other counts mean little until
    somebody explains the missing substations."""
    result = A.audit(fc([line([[6.10, 49.60], [6.15, 49.62]], "l1")]), fc([]))
    assert "no_substations_in_extract" in kinds(result)
