# SPDX-License-Identifier: Apache-2.0
"""Post-retrieval checks.

Beyond upstream parity — grid-builder has no validate stage yet. What it checks
is the failure this domain produces most quietly: a syntactically valid extract
containing nothing, which downstream becomes a power grid with no substations
and reads as a modelling result rather than a missing download.
"""

from __future__ import annotations

from dataclasses import dataclass

from _gridbuilder_tools import retrieve


@dataclass(frozen=True)
class Verdict:
    elements: int
    ok: bool
    detail: str


def validate_extract(csv_path: str, min_elements: int = 1) -> Verdict:
    """Check an extract has at least *min_elements* rows."""
    elements = retrieve.count_rows(csv_path)
    if elements >= min_elements:
        return Verdict(elements, True, f"{elements} element(s)")
    return Verdict(
        elements,
        False,
        f"only {elements} element(s), expected at least {min_elements} — "
        "the download succeeded but produced nothing usable "
        "(check the region code and that this feature exists in that extract)",
    )
