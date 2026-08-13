# SPDX-License-Identifier: Apache-2.0
"""Deterministic offline extracts, so the suite runs with no network.

Shaped like earth_osm's real output (an `id` column plus power tags) but tiny
and fixed, so tests assert on exact counts rather than on whatever OSM happens
to contain today.
"""

from __future__ import annotations

import json

from _gridbuilder_tools import storage as _storage

#: (country, feature) -> row count. Distinct values so a test that mixes up
#: which extract it is looking at fails instead of coincidentally passing.
MOCK_ELEMENTS = {
    ("BE", "substation"): 3,
    ("BE", "line"): 2,
    ("LU", "substation"): 1,
    ("LU", "line"): 1,
}
DEFAULT_ELEMENTS = 2


def element_count(country: str, feature: str) -> int:
    return MOCK_ELEMENTS.get((country, feature), DEFAULT_ELEMENTS)


def write_extract(csv_path: str, geojson_path: str, country: str, feature: str) -> None:
    """Write a mock CSV + GeoJSON pair for one (country, feature)."""
    n = element_count(country, feature)
    lines = ["id,power,voltage,tags"]
    features = []
    for i in range(n):
        osm_id = f"{country}-{feature}-{i}"
        lines.append(f"{osm_id},{feature},380000,\"{{}}\"")
        features.append(
            {
                "type": "Feature",
                "properties": {"id": osm_id, "power": feature, "voltage": "380000"},
                "geometry": {"type": "Point", "coordinates": [4.35 + i * 0.01, 50.85]},
            }
        )
    # Through the storage backend, so the offline path exercises the same
    # write route as the real one — a mock that only works on local disk would
    # hide exactly the fleet bug this package had.
    fs = _storage.get_storage(str(csv_path))
    fs.write_text(str(csv_path), "\n".join(lines) + "\n")
    fs.write_text(
        str(geojson_path),
        json.dumps({"type": "FeatureCollection", "features": features}, indent=1),
    )
