# SPDX-License-Identifier: Apache-2.0
"""Render retrieved grid features as a self-contained web map.

Upstream `grid-builder` ships a `figures/map_europe.png` in its README but no
rule that produces a map — the workflow's output is model-ready CSV/GeoJSON for
PyPSA-Eur. This is therefore **beyond parity**, and it exists because a grid
extract is one of those artifacts where looking at it catches things no row
count will: substations clustered on one motorway junction, a country whose
lines stop at an old border, an extract that silently covered the wrong region.

The GeoJSON is embedded in the page rather than fetched, so the file works from
`file://`, from the local gallery (`fw svc maps`), and from an object store —
no CORS, no second request, no dependency on where it ends up. Only the basemap
tiles come from the network.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from _gridbuilder_tools import storage as _storage

logger = logging.getLogger(__name__)

#: Per-feature styling. Substations are points/areas, lines are the network.
_STYLE = {
    "substation": {"colour": "#e4572e", "label": "Substations"},
    "line": {"colour": "#2e86ab", "label": "Transmission lines"},
    "cable": {"colour": "#7d5ba6", "label": "Cables"},
    "generator": {"colour": "#f5a623", "label": "Generators"},
    "findings": {"colour": "#111111", "label": "Audit findings"},
}
_FALLBACK = {"colour": "#555555", "label": "Other"}


def _feature_count(fc: dict) -> int:
    return len(fc.get("features") or [])


def load_layers(geojson_paths: list[str]) -> list[dict]:
    """Read each GeoJSON into a layer dict, skipping what cannot be read.

    A missing or unparseable layer is skipped with a warning rather than
    failing the render: a map of three features out of four is still worth
    looking at, and the count in the legend shows what arrived.
    """
    layers = []
    for path in geojson_paths:
        fs = _storage.get_storage(path)
        if not fs.exists(path):
            logger.warning("layer missing, skipping: %s", path)
            continue
        try:
            fc = json.loads(fs.read_text(path))
        except ValueError:
            logger.warning("layer is not valid GeoJSON, skipping: %s", path)
            continue
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # LU_substation
        country, _, feature = stem.partition("_")
        style = _STYLE.get(feature, _FALLBACK)
        layers.append(
            {
                "id": stem,
                "country": country,
                "feature": feature or "other",
                "colour": style["colour"],
                "label": f"{style['label']} — {country}",
                "count": _feature_count(fc),
                "data": fc,
            }
        )
    return layers


def build_map(
    geojson_paths: list[str],
    dest: str,
    *,
    title: str = "OpenStreetMap power grid",
    attribution: str = "© OpenStreetMap contributors (ODbL)",
) -> tuple[str, int]:
    """Render *geojson_paths* into one HTML map at *dest*.

    Returns ``(dest, total_features)``.
    """
    layers = load_layers(geojson_paths)
    if not layers:
        # An empty map is indistinguishable from a working one that found
        # nothing — which is this domain's quietest failure mode.
        raise ValueError(
            f"no readable GeoJSON layers among {len(geojson_paths)} path(s) — nothing to draw"
        )
    total = sum(layer["count"] for layer in layers)
    html = _render_html(layers, title=title, attribution=attribution, total=total)
    fs = _storage.get_storage(dest)
    parent = dest.rsplit("/", 1)[0] if "/" in dest else ""
    if parent:
        fs.mkdir_p(parent)
    fs.write_text(dest, html)
    logger.info("map: %d feature(s) across %d layer(s) → %s", total, len(layers), dest)
    return dest, total


def _render_html(layers: list[dict], *, title: str, attribution: str, total: int) -> str:
    payload = json.dumps(
        [
            {k: layer[k] for k in ("id", "label", "colour", "count", "feature", "data")}
            for layer in layers
        ],
        ensure_ascii=False,   # the page is UTF-8; \uXXXX escapes only hurt readability
    )
    # An OSM name legitimately containing "</script>" would otherwise close the
    # block and drop the rest of the page into the document. Escaping the slash
    # is inert to JSON and defuses it.
    payload = payload.replace("</", "<\\/")
    countries = sorted({layer["country"] for layer in layers})
    subtitle = f"{total:,} features · {', '.join(countries)}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body {{ margin: 0; height: 100%; font: 14px/1.4 system-ui, sans-serif; }}
  #map {{ position: absolute; inset: 0; }}
  .panel {{
    position: absolute; top: 12px; left: 12px; z-index: 1000;
    background: rgba(255,255,255,.94); padding: 12px 14px; border-radius: 8px;
    box-shadow: 0 1px 6px rgba(0,0,0,.3); max-width: 300px;
  }}
  .panel h1 {{ font-size: 15px; margin: 0 0 2px; }}
  .panel .sub {{ color: #666; font-size: 12px; margin-bottom: 8px; }}
  .panel label {{ display: block; margin: 3px 0; cursor: pointer; }}
  .swatch {{ display: inline-block; width: 11px; height: 11px; border-radius: 2px;
             margin-right: 6px; vertical-align: -1px; }}
  .count {{ color: #888; }}
  .note {{ margin-top: 9px; font-size: 11px; color: #777; }}
</style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h1>{title}</h1>
  <div class="sub">{subtitle}</div>
  <div id="legend"></div>
  <div class="note">{attribution}</div>
</div>
<script>
const LAYERS = {payload};
const map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 18, attribution: '{attribution}'
}}).addTo(map);

const bounds = L.latLngBounds([]);
const legend = document.getElementById('legend');

for (const spec of LAYERS) {{
  const layer = L.geoJSON(spec.data, {{
    style: () => ({{ color: spec.colour, weight: 2, opacity: 0.85 }}),
    pointToLayer: (f, latlng) => L.circleMarker(latlng, {{
      radius: 4, color: spec.colour, weight: 1, fillOpacity: 0.7
    }}),
    onEachFeature: (f, lyr) => {{
      const p = f.properties || {{}};
      // Only the tags that say what a thing IS — a full tag dump is unreadable
      // and these extracts carry dozens per feature.
      const rows = ['id', 'name', 'operator', 'voltage', 'power']
        .filter(k => p[k] !== undefined && p[k] !== '')
        .map(k => `<b>${{k}}</b>: ${{String(p[k]).slice(0, 60)}}`);
      lyr.bindPopup(rows.join('<br>') || spec.label);
    }}
  }}).addTo(map);
  if (layer.getBounds().isValid()) bounds.extend(layer.getBounds());

  const id = 'chk_' + spec.id;
  legend.insertAdjacentHTML('beforeend',
    `<label><input type="checkbox" id="${{id}}" checked>` +
    `<span class="swatch" style="background:${{spec.colour}}"></span>` +
    `${{spec.label}} <span class="count">(${{spec.count.toLocaleString()}})</span></label>`);
  document.getElementById(id).addEventListener('change', e => {{
    e.target.checked ? map.addLayer(layer) : map.removeLayer(layer);
  }});
}}

// Fit to the data rather than a hardcoded view: the same page serves one
// country or a continent.
bounds.isValid() ? map.fitBounds(bounds.pad(0.05)) : map.setView([50, 10], 4);
</script>
</body>
</html>
"""
