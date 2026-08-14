# SPDX-License-Identifier: Apache-2.0
"""Quality and gap audit for an OSM transmission grid extract.

An extract can be complete, well-formed, and still unusable as a network: lines
that stop in a field, a voltage nobody tagged, two mappings of the same circuit,
a region that is electrically an island because one connecting line is missing.
None of that shows up in a row count, and none of it needs a power-flow solve —
it is graph and geometry over what `grid.osm.Retrieve` already produced.

Stdlib only (haversine + grid snapping, no shapely), so the audit runs anywhere
the retrieval does.

**The tolerance is the whole game.** OSM does not guarantee that a line's
endpoint is the same node as the substation it enters, so "connected" has to
mean "within `tolerance_m`". Too tight and every junction looks dangling; too
loose and genuinely separate assets merge and the islands disappear. The default
(120 m) is deliberately conservative — it under-reports dangling ends rather
than inventing them, because a false "this grid is broken" costs more trust than
a missed one. Every finding records the tolerance it was judged at.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from _gridbuilder_tools import storage as _storage

logger = logging.getLogger(__name__)

TOOL_VERSION = "1.0"

#: Metres. See the module docstring — this is the load-bearing parameter.
DEFAULT_TOLERANCE_M = 120.0

#: A transformer lives in a substation. Two lines of different voltage meeting
#: anywhere else is either a mistagged voltage or a missing substation.
_TRANSITION_RATIO = 1.5

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Finding:
    """One problem, with somewhere to look."""

    kind: str
    severity: str
    detail: str
    lon: float
    lat: float
    refs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Geometry — small, explicit, no dependencies
# ---------------------------------------------------------------------------


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres between two (lon, lat) points."""
    lon1, lat1, lon2, lat2 = (*a, *b)
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _cell(pt: tuple[float, float], tolerance_m: float) -> tuple[int, int]:
    """Snap a point to a grid cell roughly `tolerance_m` across.

    Snapping is what makes "same node" decidable without an index. It is
    approximate near the poles, which does not matter for a power grid.
    """
    deg = tolerance_m / 111_320.0
    return (int(round(pt[0] / deg)), int(round(pt[1] / deg)))


def _line_ends(geom: dict) -> list[tuple[float, float]]:
    """First and last coordinate of a LineString / MultiLineString."""
    t = (geom or {}).get("type")
    c = (geom or {}).get("coordinates") or []
    if t == "LineString" and len(c) >= 2:
        return [tuple(c[0][:2]), tuple(c[-1][:2])]
    if t == "MultiLineString":
        ends = []
        for part in c:
            if len(part) >= 2:
                ends += [tuple(part[0][:2]), tuple(part[-1][:2])]
        return ends
    return []


def _all_points(geom: dict) -> list[tuple[float, float]]:
    """Every coordinate in any geometry, flattened."""
    c = (geom or {}).get("coordinates")
    out: list[tuple[float, float]] = []

    def walk(node):
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)) and len(node) >= 2:
                out.append((float(node[0]), float(node[1])))
            else:
                for item in node:
                    walk(item)

    walk(c)
    return out


def _centroid(geom: dict) -> tuple[float, float] | None:
    pts = _all_points(geom)
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _length_m(geom: dict) -> float:
    pts = _all_points(geom)
    return sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def parse_voltage(value: Any) -> int | None:
    """Highest voltage in an OSM `voltage` tag, or None.

    OSM writes `380000`, `380000;110000`, `"380 kV"`, or nothing. The maximum
    is the right reading for "what class of line is this".
    """
    if value is None:
        return None
    best: int | None = None
    for part in str(value).replace(",", ";").split(";"):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            continue
        v = int(digits)
        if "kv" in part.lower() and v < 10_000:
            v *= 1000
        if 1_000 <= v <= 1_500_000 and (best is None or v > best):
            best = v
    return best


def _prop(feature: dict, *names: str) -> Any:
    props = feature.get("properties") or {}
    for n in names:
        if props.get(n) not in (None, ""):
            return props[n]
    return None


def _ref(feature: dict) -> str:
    return str(_prop(feature, "id", "osm_id", "@id") or "?")


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def audit(
    lines_geojson: dict,
    substations_geojson: dict,
    *,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
) -> dict[str, Any]:
    """Findings + summary for one country's lines and substations."""
    lines = [f for f in (lines_geojson.get("features") or []) if _line_ends(f.get("geometry"))]
    subs = substations_geojson.get("features") or []

    # Substations by cell, including their footprint: a big switchyard is a
    # polygon, and a line entering the far corner is still connected to it.
    sub_cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, s in enumerate(subs):
        for pt in _all_points(s.get("geometry")) or []:
            sub_cells[_cell(pt, tolerance_m)].append(i)
        c = _centroid(s.get("geometry"))
        if c:
            sub_cells[_cell(c, tolerance_m)].append(i)

    def near_substation(pt: tuple[float, float]) -> bool:
        cx, cy = _cell(pt, tolerance_m)
        return any(
            (cx + dx, cy + dy) in sub_cells for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        )

    # Two indexes, because "connected" and "shares an endpoint" are different
    # questions. A line frequently tees into the MIDDLE of another — that is a
    # connection, so connectivity indexes every vertex; while duplicate-circuit
    # and voltage-transition checks are about what meets AT an end.
    endpoint_cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    vertex_cells: dict[tuple[int, int], set[int]] = defaultdict(set)
    line_ends: list[list[tuple[float, float]]] = []
    for idx, ln in enumerate(lines):
        ends = _line_ends(ln.get("geometry"))
        line_ends.append(ends)
        for pt in ends:
            endpoint_cells[_cell(pt, tolerance_m)].append(idx)
        for pt in _all_points(ln.get("geometry")):
            vertex_cells[_cell(pt, tolerance_m)].add(idx)

    def lines_near(pt: tuple[float, float]) -> set[int]:
        """Lines with ANY vertex in the neighbourhood of *pt*."""
        cx, cy = _cell(pt, tolerance_m)
        out: set[int] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out |= vertex_cells.get((cx + dx, cy + dy), set())
        return out

    findings: list[Finding] = []

    # --- nothing to audit is not a clean bill of health --------------------
    # Found by sweeping eight countries: Monaco returned 0 lines and the audit
    # reported 0 findings, which reads exactly like a healthy grid. An empty
    # extract is the loudest possible result, not the quietest.
    if not lines:
        c = _centroid((subs[0] or {}).get("geometry")) if subs else None
        findings.append(
            Finding(
                kind="no_lines_in_extract",
                severity="high",
                detail=(
                    f"no power lines at all ({len(subs)} substation(s) present) — either the "
                    "extract failed, the region genuinely has none, or its network is mapped "
                    "as power=cable rather than power=line and was not retrieved"
                ),
                lon=(c or (0.0, 0.0))[0],
                lat=(c or (0.0, 0.0))[1],
            )
        )
    if lines and not subs:
        pt = line_ends_first = _line_ends(lines[0].get("geometry"))[0]
        findings.append(
            Finding(
                kind="no_substations_in_extract",
                severity="high",
                detail=(
                    f"{len(lines)} line(s) and no substations — every line will read as "
                    "dangling, so the other findings are unreliable until this is explained"
                ),
                lon=pt[0],
                lat=pt[1],
            )
        )

    # --- dangling ends: a line stops where nothing else is ------------------
    for idx, ends in enumerate(line_ends):
        for pt in ends:
            if not (lines_near(pt) - {idx}) and not near_substation(pt):
                findings.append(
                    Finding(
                        kind="dangling_line_end",
                        severity="high",
                        detail=(
                            f"line ends with no other line and no substation within "
                            f"{tolerance_m:.0f} m — the network cannot carry power past here"
                        ),
                        lon=pt[0],
                        lat=pt[1],
                        refs=[_ref(lines[idx])],
                    )
                )

    # --- missing voltage ----------------------------------------------------
    voltages: list[int | None] = []
    for ln in lines:
        v = parse_voltage(_prop(ln, "voltage", "tags.voltage"))
        voltages.append(v)
        if v is None:
            c = _centroid(ln.get("geometry"))
            findings.append(
                Finding(
                    kind="missing_voltage",
                    severity="medium",
                    detail="no usable voltage tag — the line cannot be assigned a network layer",
                    lon=(c or (0.0, 0.0))[0],
                    lat=(c or (0.0, 0.0))[1],
                    refs=[_ref(ln)],
                )
            )

    # --- implausible transitions: different voltages meeting outside a sub ---
    for cell, idxs in endpoint_cells.items():
        if len(idxs) < 2:
            continue
        vs = {voltages[i] for i in idxs if voltages[i] is not None}
        if len(vs) < 2:
            continue
        lo, hi = min(vs), max(vs)
        if hi / lo < _TRANSITION_RATIO:
            continue
        pt = next(
            (p for i in idxs for p in line_ends[i] if _cell(p, tolerance_m) == cell), None
        )
        if pt is None or near_substation(pt):
            continue  # a transformer here is exactly right
        findings.append(
            Finding(
                kind="implausible_voltage_transition",
                severity="high",
                detail=(
                    f"{lo:,} V meets {hi:,} V with no substation within "
                    f"{tolerance_m:.0f} m — a transformer is missing, or a voltage is wrong"
                ),
                lon=pt[0],
                lat=pt[1],
                refs=sorted({_ref(lines[i]) for i in idxs}),
            )
        )

    # --- duplicate circuits -------------------------------------------------
    by_endpoints: dict[tuple, list[int]] = defaultdict(list)
    for idx, ends in enumerate(line_ends):
        if len(ends) >= 2:
            key = tuple(sorted([_cell(ends[0], tolerance_m), _cell(ends[-1], tolerance_m)]))
            by_endpoints[key].append(idx)
    for _key, idxs in by_endpoints.items():
        if len(idxs) < 2:
            continue
        lengths = [_length_m(lines[i].get("geometry")) for i in idxs]
        if max(lengths) <= 0 or (min(lengths) / max(lengths)) < 0.9:
            continue  # same endpoints but a different route — a real parallel path
        pt = line_ends[idxs[0]][0]
        findings.append(
            Finding(
                kind="duplicate_circuit",
                severity="low",
                detail=(
                    f"{len(idxs)} lines share both endpoints with near-identical length "
                    f"— double-mapped, or genuinely parallel circuits worth confirming"
                ),
                lon=pt[0],
                lat=pt[1],
                refs=sorted({_ref(lines[i]) for i in idxs}),
            )
        )

    # --- islands: connected components over the endpoint graph --------------
    parent = list(range(len(lines)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for cell in vertex_cells:
        # Neighbouring cells too, or a pair straddling a cell edge reads as two
        # islands — an artefact of the grid, not of the network.
        near = sorted(
            {
                j
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for j in vertex_cells.get((cell[0] + dx, cell[1] + dy), set())
            }
        )
        for j in near[1:]:
            union(near[0], j)

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(len(lines)):
        components[find(i)].append(i)
    sizes = sorted((len(v) for v in components.values()), reverse=True)
    for root, members in components.items():
        if not sizes or len(members) >= max(1, sizes[0]):
            continue  # the main network
        if len(members) > 5:
            continue  # a substantial sub-network, reported in the summary only
        pt = line_ends[members[0]][0]
        findings.append(
            Finding(
                kind="grid_island",
                severity="medium",
                detail=(
                    f"{len(members)} line(s) connected to nothing else — an island, "
                    f"or the extract is missing what joins it to the network"
                ),
                lon=pt[0],
                lat=pt[1],
                refs=sorted({_ref(lines[i]) for i in members})[:8],
            )
        )

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.kind))
    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.kind] += 1

    # Not all substations are network nodes. OSM tags street-level transformer
    # boxes `substation=minor_distribution`, and Malta has 119 of those against
    # 5 transmission substations — so "226 substations" describes the mapping
    # effort, not the grid, and a connectivity figure read without this is
    # misleading.
    classes: dict[str, int] = defaultdict(int)
    for sub in subs:
        classes[str(_prop(sub, "substation", "tags.substation") or "unclassified")] += 1

    summary = {
        "lines": len(lines),
        "substations": len(subs),
        "substation_classes": dict(sorted(classes.items(), key=lambda kv: -kv[1])),
        "transmission_substations": classes.get("transmission", 0),
        "findings": len(findings),
        "by_kind": dict(counts),
        "islands": len(components),
        "largest_island_lines": sizes[0] if sizes else 0,
        "lines_without_voltage": sum(1 for v in voltages if v is None),
        "tolerance_m": tolerance_m,
        "tool_version": TOOL_VERSION,
    }
    return {"findings": [f.as_dict() for f in findings], "summary": summary}


def findings_geojson(findings: list[dict]) -> dict:
    """Findings as points, so they can be drawn beside the grid they describe."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "id": ", ".join(f.get("refs") or []) or f["kind"],
                    "kind": f["kind"],
                    "severity": f["severity"],
                    "detail": f["detail"],
                },
                "geometry": {"type": "Point", "coordinates": [f["lon"], f["lat"]]},
            }
            for f in findings
        ],
    }


def merge_collections(collections: list[dict]) -> dict:
    """One FeatureCollection from several.

    Overhead lines and underground cables are ONE electrical network — a cable
    leaving a substation and an overhead line arriving at it are connected, and
    auditing them separately reports both as dangling. Malta made this concrete:
    226 substations against 7 `power=line` features and 43% connectivity, on a
    grid that is mostly `power=cable` and not broken at all.
    """
    return {
        "type": "FeatureCollection",
        "features": [f for c in collections for f in (c.get("features") or [])],
    }


def audit_paths(
    lines_path: str | list[str],
    substations_path: str | list[str],
    *,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    missing_ok: bool = True,
) -> dict[str, Any]:
    """Audit GeoJSON files, read through the storage backend.

    Either argument may be a list — several conductor layers (line + cable) and
    several substation layers merge into one network before analysis.

    ``missing_ok`` skips a layer that is not there, because asking for `cable`
    in a country that has none should not fail the audit; a single path that is
    missing still raises, since that is a caller error rather than an absence.
    """
    def _read(path: str) -> dict | None:
        fs = _storage.get_storage(path)
        if not fs.exists(path):
            return None
        return json.loads(fs.read_text(path))

    def _load(paths: str | list[str], what: str) -> dict:
        many = [paths] if isinstance(paths, str) else list(paths)
        loaded = []
        for path in many:
            got = _read(path)
            if got is None:
                if len(many) == 1 or not missing_ok:
                    raise FileNotFoundError(f"not found: {path}")
                logger.info("%s layer absent, skipping: %s", what, path)
                continue
            loaded.append(got)
        return merge_collections(loaded)

    return audit(
        _load(lines_path, "conductor"),
        _load(substations_path, "substation"),
        tolerance_m=tolerance_m,
    )
