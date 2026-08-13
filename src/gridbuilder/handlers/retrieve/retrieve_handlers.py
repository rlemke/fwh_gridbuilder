# SPDX-License-Identifier: Apache-2.0
"""Handler for ``grid.osm.Retrieve`` — a thin dispatcher.

All substantive work is in `_gridbuilder_tools.retrieve`; this layer coerces
parameters and shapes the return value. That keeps the terminal and the runtime
on one code path, and makes the handler testable without a runtime.
"""

from __future__ import annotations

from typing import Any

from ..shared.gridbuilder_utils import retrieve as _retrieve

NAMESPACE = "grid.osm"


def handle_retrieve(payload: dict[str, Any]) -> dict[str, Any]:
    step_log = payload.get("_step_log")
    country = payload["country"]
    feature = payload["feature"]

    result = _retrieve.retrieve_feature(
        country,
        feature,
        payload["out_dir"],
        primary_name=payload.get("primary_name") or _retrieve.DEFAULT_PRIMARY,
        source=payload.get("source") or "geofabrik",
        force=bool(payload.get("force", False)),
        # Upstream exposes these four in config/config.yaml; the FFL defaults
        # match its defaults, so a comparison starts from the same settings.
        # Default OFF: the runner is multi-threaded and earth-osm's mp forks.
        # A fork from a thread-holding-a-lock deadlocks the child, and the
        # parent then waits on it forever (seen on the fleet).
        mp=bool(payload.get("mp", False)),
        stream_backend=bool(payload.get("stream_backend", True)),
        cache_primary=bool(payload.get("cache_primary", False)),
        target_date=(payload.get("target_date") or None),
        use_mock=bool(payload.get("use_mock", False)),
    )

    if callable(step_log):
        verb = "reused cached" if result.was_cached else "extracted"
        step_log(f"{country}/{feature}: {verb} {result.elements} element(s)", "success")

    # `result` is the FeatureSet schema declared in gridbuilder.ffl.
    return {"result": result.as_dict()}


_DISPATCH = {f"{NAMESPACE}.Retrieve": handle_retrieve}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint — one per module, dispatching on _facet_name.

    One entrypoint rather than one per facet: the dispatcher's cache key does
    not include the entrypoint, so several facets registered against the same
    module with different entrypoints collide.
    """
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
            facet_name=facet,
            module_uri=f"file://{__file__}",
            entrypoint="handle",
        )
