# SPDX-License-Identifier: Apache-2.0
"""fwh_gridbuilder — power-grid models from OpenStreetMap, in FFL.

The Facetwork equivalent of PyPSA's `grid-builder` Snakemake workflow
(https://github.com/PyPSA/grid-builder), using the same `earth_osm` extractor so
the comparison is between orchestrators rather than between payloads.
"""

from __future__ import annotations

from pathlib import Path

from facetwork.domains import DomainPackage

from .handlers import register_all_registry_handlers

domain = DomainPackage(
    name="gridbuilder",
    ffl_dir=Path(__file__).parent / "ffl",
    register_handlers=register_all_registry_handlers,
)
