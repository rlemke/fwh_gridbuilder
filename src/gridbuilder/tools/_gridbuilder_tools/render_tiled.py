# SPDX-License-Identifier: Apache-2.0
"""Continent- and world-scale grid maps as vector tiles.

`render.build_map` embeds every feature in the page. That is right for a
country and impossible for a continent: South America's line layer alone is
100 MB of GeoJSON. Here each region becomes one PMTiles file (all its layers),
and one MapLibre page reads them all by HTTP range request — which static hosts
including GitHub Pages serve.

Two sizing decisions, both stated because both change what a viewer sees:

* **Low zooms show the backbone, not a sample.** Each feature gets a tippecanoe
  ``minzoom`` from its voltage, so ≥ 220 kV lines are drawn from z0 and
  distribution only from z8. Leaving it to tippecanoe's density dropping would
  thin the map randomly, which reads as missing grid.
* **One file per region stays under a host limit.** GitHub rejects files over
  100 MB, so a region whose tiles exceed ``max_bytes`` is re-tiled one zoom
  shallower, and the page records the maxzoom it actually got.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from _gridbuilder_tools import storage as _storage
from _gridbuilder_tools.audit import parse_voltage

logger = logging.getLogger(__name__)

LAYERS = ("line", "cable", "substation")
DEFAULT_MAX_BYTES = 95_000_000
DEFAULT_MAXZOOM = 12
MIN_MAXZOOM = 8


@dataclass(frozen=True)
class TiledRegion:
    region: str
    pmtiles: str
    bytes: int
    maxzoom: int
    features: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def voltage_kv(props: dict) -> int | None:
    """Highest voltage in kV, from earth_osm's flattened ``tags.voltage``."""
    volts = parse_voltage(props.get("tags.voltage") or props.get("voltage"))
    return None if volts is None else int(round(volts / 1000))


def minzoom_for(layer: str, kv: int | None) -> int:
    """The zoom a feature first appears at. Unknown voltage is treated as low.

    Never deeper than ``MIN_MAXZOOM``: a tileset retiled down to that zoom must
    still contain every feature (tippecanoe silently drops one whose minzoom is
    past the maxzoom it builds)."""
    kv = kv or 0
    if layer == "substation":
        return 3 if kv >= 220 else 5 if kv >= 110 else 7 if kv >= 33 else MIN_MAXZOOM
    return 0 if kv >= 220 else 3 if kv >= 110 else 5 if kv >= 33 else 8


def slim_feature(layer: str, feature: dict) -> dict | None:
    """Keep what the map shows; earth_osm's ``refs`` dominate the bytes."""
    geom = feature.get("geometry")
    if not geom:
        return None
    props = feature.get("properties") or {}
    kv = voltage_kv(props)
    out = {"id": props.get("id"), "kv": kv if kv is not None else -1}
    for src, dst in (("tags.name", "name"), ("tags.operator", "operator"),
                     ("tags.substation", "class"), ("tags.voltage", "voltage")):
        if props.get(src):
            out[dst] = props[src]
    return {"type": "Feature", "geometry": geom, "properties": out,
            "tippecanoe": {"minzoom": minzoom_for(layer, kv)}}


def layer_of(path: str) -> str | None:
    stem = Path(path).name.removesuffix(".geojson")
    return next((lyr for lyr in LAYERS if stem.endswith("_" + lyr)), None)


def tile_region(
    region: str,
    geojson_paths: list[str],
    dest: str,
    *,
    maxzoom: int = DEFAULT_MAXZOOM,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TiledRegion:
    """One region's layers → one PMTiles at ``dest`` (local or s3://)."""
    if shutil.which("tippecanoe") is None:
        raise RuntimeError("tippecanoe is not installed on this host")
    with tempfile.TemporaryDirectory(prefix="gridbuilder-tiles-") as tmp:
        counts: dict[str, int] = {}
        inputs: list[str] = []
        for path in geojson_paths:
            layer = layer_of(path)
            if layer is None:
                logger.warning("%s: not a line/cable/substation layer — skipped", path)
                continue
            local = Path(tmp) / f"{layer}.src.geojson"
            _storage.get_storage(path).fetch_to_local(path, str(local))
            with open(local, encoding="utf-8") as fh:
                feats = json.load(fh).get("features") or []
            local.unlink()
            slim = Path(tmp) / f"{layer}.geojsonseq"
            n = 0
            with open(slim, "w", encoding="utf-8") as out:
                for f in feats:
                    s = slim_feature(layer, f)
                    if s is not None:
                        out.write(json.dumps(s, separators=(",", ":")) + "\n")
                        n += 1
            del feats
            counts[layer] = counts.get(layer, 0) + n
            inputs += ["-L", json.dumps({"file": str(slim), "layer": layer})]
        if not inputs:
            raise ValueError(f"{region}: no line/cable/substation GeoJSON among {geojson_paths}")

        out = Path(tmp) / f"{region}.pmtiles"
        z = maxzoom
        while True:
            subprocess.run(
                ["tippecanoe", "-o", str(out), "--force", "--quiet", "-Z0", f"-z{z}",
                 # name/description default to the temp path — never publish that
                 "-n", f"{region} power grid", "-N", "OpenStreetMap power infrastructure (ODbL)",
                 "--drop-densest-as-needed", "--extend-zooms-if-still-dropping",
                 "--read-parallel", *inputs],
                check=True, capture_output=True, text=True,
            )
            size = out.stat().st_size
            if size <= max_bytes or z <= MIN_MAXZOOM:
                break
            logger.info("%s: %.0f MB at z%d exceeds %.0f MB — retiling at z%d",
                        region, size / 1e6, z, max_bytes / 1e6, z - 1)
            z -= 1
        if size > max_bytes:
            raise RuntimeError(f"{region}: {size / 1e6:.0f} MB even at z{z}; split the region")
        _storage.get_storage(dest).finalize_from_local(str(out), dest)
    logger.info("%s: %s (%.1f MB, z0-%d, %s)", region, dest, size / 1e6, z, counts)
    return TiledRegion(region, dest, size, z, counts)


def build_page(tiled: list[dict], dest: str, *, title: str,
               totals: dict[str, int] | None = None) -> str:
    """The MapLibre page. PMTiles are referenced RELATIVE to the page, so the
    directory publishes as a unit."""
    sources = [
        {"region": t["region"], "url": Path(t["pmtiles"]).name, "maxzoom": int(t["maxzoom"])}
        for t in tiled
    ]
    # Continent extracts overlap at their borders, so per-region counts summed
    # double-count what crosses one. A caller holding de-duplicated counts
    # passes them; otherwise the page says the sum is an upper bound.
    note = ""
    if totals is None:
        totals = {}
        for t in tiled:
            for k, v in (t.get("features") or {}).items():
                totals[k] = totals.get(k, 0) + int(v)
        if len(tiled) > 1:
            note = " (summed per region; features crossing a region border count in each)"
    html = (_PAGE
            .replace("__TITLE__", _esc(title))
            .replace("__SOURCES__", json.dumps(sources))
            .replace("__TOTALS__", json.dumps(totals))
            .replace("__NOTE__", _esc(note)))
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(html)
        local = fh.name
    Path(local).chmod(0o644)  # mkstemp is 0600; a served page must be world-readable
    _storage.get_storage(dest).finalize_from_local(local, dest)
    return dest


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<script src="https://unpkg.com/pmtiles@3.2.1/dist/pmtiles.js"></script>
<style>
html,body,#map{margin:0;height:100%;background:#1b1d22}
#panel{position:absolute;top:10px;left:10px;z-index:2;max-width:300px;background:rgba(255,255,255,.94);
 font:13px/1.4 system-ui,sans-serif;padding:10px 12px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3)}
#panel h1{font-size:15px;margin:0 0 6px}#panel label{display:block;cursor:pointer}
.sw{display:inline-block;width:18px;height:4px;margin-right:6px;vertical-align:middle}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 10px 0 4px;vertical-align:middle}
#panel small{color:#555}
</style></head><body><div id="map"></div>
<div id="panel"><h1>__TITLE__</h1>
<label><input type="checkbox" data-layer="line" checked> <span class="sw" style="background:#e8432e"></span>≥ 220 kV</label>
<label><input type="checkbox" data-layer="line" checked> <span class="sw" style="background:#f39c12"></span>110–219 kV</label>
<label><input type="checkbox" data-layer="line" checked> <span class="sw" style="background:#3aa0ff"></span>33–109 kV</label>
<label><input type="checkbox" data-layer="line" checked> <span class="sw" style="background:#a3e635"></span>&lt; 33 kV / unknown</label>
<label><input type="checkbox" data-layer="cable" checked> <span class="sw" style="background:repeating-linear-gradient(90deg,#ff4fd8 0 4px,transparent 4px 7px)"></span>Underground / submarine cable</label>
<label><input type="checkbox" data-layer="substation" checked> <span class="dot" style="background:#111;border:1px solid #fff"></span>Substation</label>
<small id="stats"></small><br>
<small>Lines appear by voltage as you zoom in: transmission first, distribution from z8.
Data © OpenStreetMap contributors (ODbL), extracted with earth_osm.</small></div>
<script>
const SOURCES = __SOURCES__, TOTALS = __TOTALS__;
document.getElementById('stats').textContent =
  Object.entries(TOTALS).map(([k,v]) => v.toLocaleString() + ' ' + k + (v === 1 ? '' : 's')).join(' · ') + '__NOTE__';
const protocol = new pmtiles.Protocol(); maplibregl.addProtocol('pmtiles', protocol.tile);
const base = new URL('.', location.href).href;
const map = new maplibregl.Map({container: 'map', center: [10, 25], zoom: 1.6, hash: true,
  style: {version: 8, glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
    sources: {osm: {type: 'raster', tileSize: 256, maxzoom: 19,
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      attribution: '© OpenStreetMap contributors'}},
    layers: [{id: 'osm', type: 'raster', source: 'osm',
      paint: {'raster-saturation': -0.9, 'raster-brightness-max': 0.55, 'raster-contrast': -0.1}}]}});
map.addControl(new maplibregl.NavigationControl());
const kvColor = ['step', ['get', 'kv'], '#a3e635', 33, '#3aa0ff', 110, '#f39c12', 220, '#e8432e'];
const byLayer = {line: [], cable: [], substation: []};
// 'style.load', not 'load': 'load' waits for every basemap tile, so a slow or
// throttled raster server would hold back the grid itself.
map.once('style.load', () => {
  for (const s of SOURCES) {
    const id = 'src-' + s.region;
    map.addSource(id, {type: 'vector', url: 'pmtiles://' + base + s.url, maxzoom: s.maxzoom});
    const add = (lyr, def) => { map.addLayer(Object.assign({id: lyr + '-' + s.region, source: id, 'source-layer': lyr}, def));
      byLayer[lyr].push(lyr + '-' + s.region); };
    add('line', {type: 'line', layout: {'line-cap': 'round'}, paint: {'line-color': kvColor,
      'line-width': ['interpolate', ['linear'], ['zoom'], 2, ['step', ['get', 'kv'], 0.4, 220, 1.1], 10, ['step', ['get', 'kv'], 1, 110, 2.2]]}});
    add('cable', {type: 'line', paint: {'line-color': '#ff4fd8', 'line-dasharray': [2, 1.5],
      'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.6, 10, 2]}});
    add('substation', {type: 'circle', paint: {'circle-color': kvColor, 'circle-stroke-color': '#fff',
      'circle-stroke-width': 0.6, 'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 1.2, 12, 5]}});
  }
  for (const box of document.querySelectorAll('#panel input')) box.addEventListener('change', () => {
    const lyr = box.dataset.layer, on = [...document.querySelectorAll('#panel input[data-layer="' + lyr + '"]')];
    if (lyr === 'line') {
      const bands = [[-1e9, 33], [33, 110], [110, 220], [220, 1e9]].reverse().filter((_, i) => on[i].checked);
      const f = ['any', ...bands.map(([lo, hi]) => ['all', ['>=', ['get', 'kv'], lo], ['<', ['get', 'kv'], hi]])];
      // No band checked → hide the layer. A "match nothing" filter such as
      // ["==", 1, 0] parses as the LEGACY filter syntax, is rejected, and leaves
      // the previous filter in force — the last band could never be unchecked.
      for (const id of byLayer.line) {
        if (bands.length) map.setFilter(id, f);
        map.setLayoutProperty(id, 'visibility', bands.length ? 'visible' : 'none');
      }
    } else for (const id of byLayer[lyr]) map.setLayoutProperty(id, 'visibility', box.checked ? 'visible' : 'none');
  });
  const popup = new maplibregl.Popup({closeButton: false});
  const ids = () => [...byLayer.line, ...byLayer.cable, ...byLayer.substation];
  map.on('click', (e) => {
    const f = map.queryRenderedFeatures(e.point, {layers: ids()})[0]; if (!f) return;
    const p = f.properties, rows = [['', f.sourceLayer], ['name', p.name], ['voltage', p.voltage],
      ['operator', p.operator], ['class', p['class']], ['OSM', p.id]].filter(r => r[1] !== undefined && r[1] !== null);
    popup.setLngLat(e.lngLat).setHTML(rows.map(([k, v]) => (k ? '<b>' + k + '</b> ' : '') +
      String(v).replace(/[<&]/g, c => c === '<' ? '&lt;' : '&amp;')).join('<br>')).addTo(map);
  });
});
</script></body></html>
"""
