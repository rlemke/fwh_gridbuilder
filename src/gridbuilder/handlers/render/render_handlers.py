# SPDX-License-Identifier: Apache-2.0
"""Handler for ``grid.osm.BuildMap`` — a thin dispatcher."""

from __future__ import annotations

from typing import Any

from ..shared.gridbuilder_utils import render as _render

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


_DISPATCH = {f"{NAMESPACE}.BuildMap": handle_build_map}


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
