# SPDX-License-Identifier: Apache-2.0
"""Handler for ``grid.osm.ValidateRetrieval`` — a thin dispatcher."""

from __future__ import annotations

from typing import Any

from ..shared.gridbuilder_utils import validate as _validate

NAMESPACE = "grid.osm"


def handle_validate(payload: dict[str, Any]) -> dict[str, Any]:
    verdict = _validate.validate_extract(
        payload["csv_path"], int(payload.get("min_elements") or 1)
    )
    step_log = payload.get("_step_log")
    if callable(step_log):
        step_log(verdict.detail, "success" if verdict.ok else "warning")
    return {"elements": verdict.elements, "ok": verdict.ok, "detail": verdict.detail}


_DISPATCH = {f"{NAMESPACE}.ValidateRetrieval": handle_validate}


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
            facet_name=facet,
            module_uri=f"file://{__file__}",
            entrypoint="handle",
        )
