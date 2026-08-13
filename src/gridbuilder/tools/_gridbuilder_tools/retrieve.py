# SPDX-License-Identifier: Apache-2.0
"""OSM power-infrastructure retrieval — the work behind ``grid.osm.Retrieve``.

Wraps ``earth_osm``, the same extractor PyPSA's ``grid-builder`` calls from its
``retrieve_osm.py`` script. That is deliberate: this package exists to compare
two ORCHESTRATORS, and swapping the extraction library underneath would compare
nothing. Given the same country and feature, both projects run the same code and
should produce the same rows.

What is different is everything around the call — and specifically the decision
of whether to run it at all.

``grid-builder`` states its outputs to Snakemake and lets `make` semantics
decide: the outputs exist and are newer than their inputs, so the rule is
skipped. But ``rule retrieve_osm`` declares no file inputs, so after the first
successful run there is nothing that can ever make its outputs look stale.
Re-running the workflow a year later reports "nothing to be done" and serves
year-old OSM data. The escape hatch is ``force_redownload`` in the config, set
by a human who already suspected.

Here the reuse decision is recorded and re-checked (see :func:`is_current`):
the extractor version, the source, and the identity of the extract. When any of
them moves, the artifact is stale — no timestamp involved, and no human needed
to suspect anything.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from _gridbuilder_tools import storage as _storage

logger = logging.getLogger(__name__)

#: Bump when a change here would alter the CONTENT of an artifact. It is half of
#: the cache key: an artifact written by an older version of this code must not
#: be reused by a newer one that would have produced something different.
TOOL_NAME = "gridbuilder.retrieve"
TOOL_VERSION = "1.0"

SIDECAR_SUFFIX = ".meta.json"

#: Upstream's config/config.yaml defaults, so a comparison starts from the same
#: configuration rather than a friendlier one.
DEFAULT_PRIMARY = "power"
DEFAULT_FEATURES = ("substation", "line")
VALID_SOURCES = ("geofabrik", "overpass")


@dataclass(frozen=True)
class FeatureSet:
    """One (country, feature) extraction."""

    country: str
    feature: str
    csv_path: str
    geojson_path: str
    elements: int
    was_cached: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def earth_osm_version() -> str:
    """The extractor's version — the other half of the cache key.

    Reported as ``unknown`` rather than raising when earth_osm is absent, so
    ``is_current`` can still make a decision (an artifact whose extractor cannot
    be identified is not provably current, and is therefore re-derived).
    """
    try:
        from importlib.metadata import version

        return version("earth-osm")
    except Exception:  # noqa: BLE001 — absence is a valid answer here
        return "unknown"


# ---------------------------------------------------------------------------
# Output naming — must match upstream so the two are comparable file-for-file
# ---------------------------------------------------------------------------


def output_names(country: str, feature: str) -> tuple[str, str]:
    """``(csv, geojson)`` basenames for one extraction.

    ``{country}_{feature}.csv`` / ``.geojson`` is upstream's `retrieve.smk`
    output pattern verbatim. Keeping it identical is what lets the two runs be
    diffed rather than described.
    """
    stem = f"{country}_{feature}"
    return f"{stem}.csv", f"{stem}.geojson"


def sidecar_path(artifact: str) -> str:
    return artifact + SIDECAR_SUFFIX


# ---------------------------------------------------------------------------
# The reuse decision
# ---------------------------------------------------------------------------


def read_sidecar(path: str) -> dict | None:
    """Provenance for an artifact, or None when absent or unreadable.

    Unreadable means the artifact cannot be PROVEN current, and trusting it
    anyway is the failure this module is built against.
    """
    return _storage.read_json(sidecar_path(path) if not path.endswith(SIDECAR_SUFFIX) else path)


def is_current(
    csv_path: str,
    *,
    country: str,
    feature: str,
    primary_name: str,
    source: str,
    target_date: str | None = None,
) -> tuple[bool, str]:
    """Whether the cached extraction may be reused, and why not if not.

    Every condition is about IDENTITY, never about time:

    * the artifact and its sidecar both exist (a sidecar written last means a
      half-finished run cannot look complete);
    * the recorded size matches the file on disk (a truncated write is not a
      cache hit);
    * it was produced by this tool version and this extractor version;
    * it was produced for this country/feature/primary/source, so a config
      change re-derives instead of silently serving the previous config's data.
    """
    fs = _storage.get_storage(csv_path)
    side = read_sidecar(csv_path)
    if side is None:
        return False, "no sidecar"
    if not fs.exists(csv_path):
        return False, "artifact missing"

    actual = fs.size(csv_path)
    if int(side.get("size_bytes", -1)) != actual:
        return False, f"size {actual} != recorded {side.get('size_bytes')}"
    if side.get("tool_version") != TOOL_VERSION:
        return False, f"written by {TOOL_NAME} {side.get('tool_version')}, now {TOOL_VERSION}"
    if side.get("extractor_version") != earth_osm_version():
        return False, (
            f"extracted by earth-osm {side.get('extractor_version')}, "
            f"now {earth_osm_version()}"
        )
    for key, want in (
        ("country", country),
        ("feature", feature),
        ("primary_name", primary_name),
        ("source", source),
        # A different historical snapshot is different DATA, not a different
        # way of fetching the same data — so it belongs in the identity check
        # alongside the country and feature.
        ("target_date", target_date),
    ):
        if side.get(key) != want:
            return False, f"{key} was {side.get(key)!r}, now {want!r}"
    return True, "current"


def write_sidecar(
    csv_path: str,
    *,
    country: str,
    feature: str,
    primary_name: str,
    source: str,
    elements: int,
    generated_at: str,
    target_date: str | None = None,
) -> None:
    """Record what produced this artifact. Written LAST, after the data."""
    fs = _storage.get_storage(csv_path)
    payload = {
        "country": country,
        "feature": feature,
        "primary_name": primary_name,
        "source": source,
        "target_date": target_date,
        "elements": elements,
        "size_bytes": fs.size(csv_path),
        "generated_at": generated_at,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "extractor": "earth-osm",
        "extractor_version": earth_osm_version(),
        "license": "ODbL 1.0 (OpenStreetMap contributors)",
    }
    fs.write_text(sidecar_path(csv_path), json.dumps(payload, indent=1, sort_keys=True))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def count_rows(csv_path: str) -> int:
    """Data rows in a CSV, excluding the header.

    The number that matters downstream: an extract with a header and no rows is
    a successful download of nothing, and is exactly what an existence check
    cannot distinguish from a real result.
    """
    fs = _storage.get_storage(csv_path)
    if not fs.exists(csv_path):
        return 0
    # Read once rather than stream: these extracts are megabytes, and an object
    # store has no cheap line-wise read anyway.
    rows = sum(1 for _ in csv.reader(io.StringIO(fs.read_text(csv_path))))
    return max(0, rows - 1)


def _locate_outputs(search_root: Path, country: str, feature: str) -> tuple[Path | None, Path | None]:
    """Find what earth_osm wrote.

    It composes its own directory layout (``out/<REGION>/...``) and names files
    by the region code it resolved, which is not always the code we passed —
    ``BE`` becomes ``belgium``. So the outputs are located by suffix rather than
    assumed, and the caller renames them to upstream's flat convention.
    """
    csv_hit = geojson_hit = None
    for path in sorted(search_root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        if feature.lower() not in name:
            continue
        if name.endswith(".csv") and csv_hit is None:
            csv_hit = path
        elif name.endswith(".geojson") and geojson_hit is None:
            geojson_hit = path
    return csv_hit, geojson_hit


def retrieve_feature(
    country: str,
    feature: str,
    out_dir: str,
    *,
    primary_name: str = DEFAULT_PRIMARY,
    source: str = "geofabrik",
    force: bool = False,
    mp: bool = True,
    stream_backend: bool = True,
    cache_primary: bool = False,
    target_date: str | None = None,
    data_dir: str | None = None,
    use_mock: bool = False,
) -> FeatureSet:
    """Retrieve one OSM power feature for one country.

    Returns the paths written plus the element count. ``was_cached`` says the
    extraction was skipped because the recorded provenance still matched — which
    is a claim about identity, not about mtime.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {source!r}")

    fs = _storage.get_storage(out_dir)
    fs.mkdir_p(out_dir)
    csv_name, geojson_name = output_names(country, feature)
    csv_path = _storage.Storage.join(out_dir, csv_name)
    geojson_path = _storage.Storage.join(out_dir, geojson_name)

    if not force:
        current, why = is_current(
            str(csv_path),
            country=country,
            feature=feature,
            primary_name=primary_name,
            source=source,
            target_date=target_date,
        )
        if current:
            logger.info("%s/%s: reusing cached extract (%s)", country, feature, why)
            return FeatureSet(
                country=country,
                feature=feature,
                csv_path=csv_path,
                geojson_path=geojson_path,
                elements=count_rows(csv_path),
                was_cached=True,
            )
        logger.info("%s/%s: re-extracting — %s", country, feature, why)

    if use_mock:
        from _gridbuilder_tools import gridbuilder_mocks

        gridbuilder_mocks.write_extract(csv_path, geojson_path, country, feature)  # via storage
    else:
        _run_earth_osm(
            country=country,
            feature=feature,
            primary_name=primary_name,
            source=source,
            csv_path=csv_path,
            geojson_path=geojson_path,
            data_dir=data_dir,
            force=force,
            mp=mp,
            stream_backend=stream_backend,
            cache_primary=cache_primary,
            target_date=target_date,
        )

    elements = count_rows(csv_path)
    write_sidecar(
        csv_path,
        country=country,
        feature=feature,
        primary_name=primary_name,
        source=source,
        elements=elements,
        generated_at=_utc_now(),
        target_date=target_date,
    )
    logger.info("%s/%s: %d element(s) → %s", country, feature, elements, csv_path)
    return FeatureSet(
        country=country,
        feature=feature,
        csv_path=csv_path,
        geojson_path=geojson_path,
        elements=elements,
        was_cached=False,
    )


def _parse_target_date(target_date: str | None):
    """ISO 8601 string -> datetime, which is what earth_osm expects.

    Accepted as a string at this boundary because that is what survives a
    config file, an FFL parameter and a step value; upstream does the same
    conversion in its `parse_target_date`.
    """
    if not target_date:
        return None
    from datetime import datetime

    return datetime.fromisoformat(target_date)


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _run_earth_osm(
    *,
    country: str,
    feature: str,
    primary_name: str,
    source: str,
    csv_path: str,
    geojson_path: str,
    data_dir: str | None,
    force: bool = False,
    mp: bool = True,
    stream_backend: bool = True,
    cache_primary: bool = False,
    target_date: str | None = None,
) -> None:
    """Call earth_osm and move its output to upstream's flat naming.

    Extraction runs into a temporary directory and the results are moved into
    place only on success, so an interrupted run cannot leave a partial CSV
    where the next run would find it. (The sidecar is written after the move,
    so even a crash between the two leaves an artifact with no provenance —
    which `is_current` reads as "not reusable", the safe direction.)
    """
    import earth_osm.eo as eo  # imported lazily: a heavy, optional dependency

    with tempfile.TemporaryDirectory(prefix="gridbuilder-") as tmp:
        staging = Path(tmp) / "out"
        staging.mkdir(parents=True, exist_ok=True)
        eo.save_osm_data(
            primary_name=primary_name,
            region_list=[country],
            feature_list=[feature],
            data_source=source,
            out_dir=str(staging),
            data_dir=data_dir or os.environ.get("FW_GRIDBUILDER_DATA_DIR")
            or str(Path.home() / ".cache" / "earth-osm"),
            out_format=["csv", "geojson"],
            out_aggregate=False,
            # `force` has to reach BOTH layers. Skipping only this package's
            # sidecar would re-run the extraction against the PBF already on
            # disk — a "forced" refresh that re-derives last month's download.
            # earth_osm calls the same flag `update`; upstream exposes it as
            # `force_redownload`.
            update=force,
            mp=mp,
            stream_backend=stream_backend,
            cache_primary=cache_primary,
            target_date=_parse_target_date(target_date),
        )
        found_csv, found_geojson = _locate_outputs(staging, country, feature)
        if found_csv is None:
            raise FileNotFoundError(
                f"earth_osm produced no CSV for {country}/{feature} — "
                f"check the region code and that '{primary_name}={feature}' exists in that extract"
            )
        # Finalize through the storage backend: on a fleet `csv_path` is an
        # `s3://` URI, and shutil.move would create a directory named `s3:`
        # on the container's disk, which vanishes with the container.
        fs = _storage.get_storage(str(csv_path))
        fs.finalize_from_local(str(found_csv), str(csv_path))
        if found_geojson is not None:
            fs.finalize_from_local(str(found_geojson), str(geojson_path))
        else:
            # A CSV without its GeoJSON is a partial result; say so rather than
            # leaving a downstream step to fail on a missing file.
            logger.warning("%s/%s: no GeoJSON produced alongside the CSV", country, feature)
