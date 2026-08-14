# SPDX-License-Identifier: Apache-2.0
"""Handler for ``grid.audit.AuditGrid`` — a thin dispatcher."""

from __future__ import annotations

import json
from typing import Any

from ..shared.gridbuilder_utils import audit as _audit
from ..shared.gridbuilder_utils import storage as _storage

NAMESPACE = "grid.audit"


def handle_audit(payload: dict[str, Any]) -> dict[str, Any]:
    country = payload.get("country") or ""
    result = _audit.audit_paths(
        payload["lines_path"],
        payload["substations_path"],
        tolerance_m=float(payload.get("tolerance_m") or _audit.DEFAULT_TOLERANCE_M),
    )
    out_dir = payload["out_dir"].rstrip("/")
    stem = f"{country}_" if country else ""
    fs = _storage.get_storage(out_dir)

    # Findings as points, so the map can draw them over the grid. Written
    # beside the extract they describe, not in a separate report directory —
    # a finding is only meaningful next to its data.
    findings_path = f"{out_dir}/{stem}findings.geojson"
    fs.write_text(
        findings_path, json.dumps(_audit.findings_geojson(result["findings"]), indent=1)
    )
    report_path = f"{out_dir}/{stem}audit.json"
    fs.write_text(report_path, json.dumps(result, indent=1, sort_keys=True))

    step_log = payload.get("_step_log")
    if callable(step_log):
        s = result["summary"]
        worst = s["by_kind"].get("dangling_line_end", 0)
        level = "warning" if s["findings"] else "success"
        step_log(
            f"{country or 'grid'}: {s['findings']} finding(s) over {s['lines']} lines — "
            f"{worst} dangling end(s), {s['islands']} island(s), "
            f"{s['lines_without_voltage']} without voltage",
            level,
        )
    return {
        "findings": result["findings"],
        "summary": result["summary"],
        "findings_path": findings_path,
        "report_path": report_path,
    }


_DISPATCH = {f"{NAMESPACE}.AuditGrid": handle_audit}


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
