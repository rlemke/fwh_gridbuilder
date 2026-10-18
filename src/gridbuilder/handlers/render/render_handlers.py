# SPDX-License-Identifier: Apache-2.0
"""Handler for ``grid.osm.BuildMap`` — a thin dispatcher."""

from __future__ import annotations

from typing import Any

from ..shared.gridbuilder_utils import render as _render
from ..shared.gridbuilder_utils import render_tiled as _tiled

NAMESPACE = "grid.osm"


def _geojson_paths(value: Any) -> list[str]:
    """Accept either bare paths or the FeatureSet dicts a fan-out returns.

    `BuildGridMap` passes `built.sets` straight through, so the common case is
    a list of FeatureSet dicts; a caller with paths in hand should not have to
    wrap them. Anything else raises rather than silently drawing an empty map —
    an empty map looks exactly like a working one that found nothing.
    """
    if isinstance(value, str):
        value = [value]
    paths: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, dict) and item.get("geojson_path"):
            paths.append(item["geojson_path"])
        else:
            raise ValueError(
                f"cannot read a GeoJSON path from {item!r} — pass paths, or the "
                "FeatureSet values a retrieval fan-out returns"
            )
    if not paths:
        raise ValueError("no GeoJSON paths given — nothing to draw")
    return paths


def handle_build_map(payload: dict[str, Any]) -> dict[str, Any]:
    paths = _geojson_paths(payload.get("geojson_paths"))
    dest, features = _render.build_map(
        paths,
        payload["dest"],
        title=payload.get("title") or "OpenStreetMap power grid",
    )
    step_log = payload.get("_step_log")
    if callable(step_log):
        step_log(f"drew {features:,} feature(s) from {len(paths)} layer(s) → {dest}", "success")
    return {"path": dest, "features": features}


def handle_tile_region(payload: dict[str, Any]) -> dict[str, Any]:
    paths = _geojson_paths(payload.get("geojson_paths"))
    got = _tiled.tile_region(
        payload["region"],
        paths,
        payload["dest"],
        maxzoom=int(payload.get("maxzoom") or _tiled.DEFAULT_MAXZOOM),
        max_bytes=int(payload.get("max_bytes") or _tiled.DEFAULT_MAX_BYTES),
    )
    step_log = payload.get("_step_log")
    if callable(step_log):
        step_log(f"{got.region}: {got.bytes / 1e6:.1f} MB PMTiles, z0-{got.maxzoom}, {got.features}", "success")
    return {"tiled": got.as_dict()}


def handle_build_tiled_page(payload: dict[str, Any]) -> dict[str, Any]:
    tiled = payload.get("tiled") or []
    if isinstance(tiled, dict):
        tiled = [tiled]
    if not tiled:
        raise ValueError("no tiled regions given — nothing to draw")
    dest = _tiled.build_page(tiled, payload["dest"],
                             title=payload.get("title") or "OpenStreetMap power grid")
    return {"path": dest, "regions": len(tiled)}


_DISPATCH = {
    f"{NAMESPACE}.BuildMap": handle_build_map,
    f"{NAMESPACE}.TileRegion": handle_tile_region,
    f"{NAMESPACE}.BuildTiledPage": handle_build_tiled_page,
}


def handle(payload: dict) -> dict:
    facet = payload["_facet_name"]
    fn = _DISPATCH.get(facet)
    if fn is None:
        raise KeyError(f"no handler for {facet!r} in {__name__}")
    return fn(payload)


def facet_names() -> list[str]:
    return sorted(_DISPATCH)


def register_handlers(runner) -> None:
    for facet in facet_names():
        runner.register_handler(
            facet_name=facet, module_uri=f"file://{__file__}", entrypoint="handle"
        )
