# SPDX-License-Identifier: Apache-2.0
"""Bring-your-own PBF: reduce a large extract to its power features first.

``earth_osm`` downloads its input from Geofabrik and parses every block in pure
Python. Both stop it scaling past a country:

* the fleet cannot reach Geofabrik at all, but already holds current
  continent extracts of its own;
* parse time follows the size of the INPUT, while the output (power
  infrastructure) is a fraction of a percent of it.

So this module runs ``osmium tags-filter nwr/<primary>`` over the source PBF —
keeping every object carrying the primary key plus the nodes and members those
objects reference — and hands the small result to the SAME extractor. Measured
on 2026-10-17 extracts:

=================  ========  ==========  ====================  =========
region             source    filtered    earth_osm raw → flt   rows
=================  ========  ==========  ====================  =========
Luxembourg         55 MB     0.25 MB     9.2 s → 0.8 s         1,490
Central America    911 MB    2.6 MB      148 s → 3.3 s         6,980
South America      4.6 GB    23 MB       (not run) → 30 s      52,028
=================  ========  ==========  ====================  =========

Same rows, ids and tags either way; coordinates agree to ~1e-13° (osmium
re-encodes the PBF, so float rounding differs far below OSM's 1e-7 precision).
The parity claim this domain rests on — same extractor as upstream — holds:
only the input is smaller.

Memory is the real bound, and it follows the number of matched features, not
the source size (South America: 1.45 GB peak for 52k features).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from _gridbuilder_tools import storage as _storage

logger = logging.getLogger(__name__)

TOOL_NAME = "gridbuilder.prefilter"
#: Bump when the filter expression or its semantics change.
TOOL_VERSION = "1.0"


@dataclass(frozen=True)
class Prefiltered:
    source: str
    filtered: str
    source_identity: str
    source_bytes: int
    filtered_bytes: int
    was_cached: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def filter_expression(primary_name: str) -> str:
    """``nwr/<key>`` — every node, way and relation carrying the key.

    Deliberately the whole KEY, not the requested features: one filtered file
    then serves every feature list, and towers/poles are the line's own nodes
    anyway. osmium keeps the referenced nodes of matched ways and the members of
    matched relations (no ``-R``), which is what earth_osm needs to build
    geometries."""
    if not primary_name or "/" in primary_name or "=" in primary_name:
        raise ValueError(f"primary_name must be a bare OSM key, got {primary_name!r}")
    return f"nwr/{primary_name}"


def filtered_name(source: str, primary_name: str, source_identity: str) -> str:
    """Content-addressed name: a moved source can never be served stale."""
    stem = Path(source.rstrip("/")).name
    for suffix in (".osm.pbf", ".pbf"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    digest = hashlib.sha1(source_identity.encode()).hexdigest()[:12]
    return f"{stem}.{primary_name}.{digest}.osm.pbf"


def osmium_version() -> str:
    try:
        out = subprocess.run(["osmium", "--version"], capture_output=True, text=True, check=True)
        return out.stdout.splitlines()[0].strip()
    except (OSError, subprocess.CalledProcessError, IndexError):
        return "unknown"


def prefilter(
    source: str,
    out_dir: str,
    *,
    primary_name: str = "power",
    force: bool = False,
    scratch_dir: str | None = None,
) -> Prefiltered:
    """Filter ``source`` (local path or ``s3://``) into ``out_dir``.

    Reused when a file for the same source identity, key and tool version is
    already there with its sidecar — the name itself carries the source
    identity, so a refreshed continent produces a new name rather than matching
    the old one.
    """
    src_fs = _storage.get_storage(source)
    identity = src_fs.identity(source)
    source_bytes = src_fs.size(source)
    out_fs = _storage.get_storage(out_dir)
    out_fs.mkdir_p(out_dir)
    dest = _storage.Storage.join(out_dir, filtered_name(source, primary_name, identity))
    side_path = dest + ".meta.json"

    if not force:
        side = _storage.read_json(side_path)
        if (
            side
            and out_fs.exists(dest)
            and side.get("tool_version") == TOOL_VERSION
            and side.get("source_identity") == identity
            and int(side.get("size_bytes", -1)) == out_fs.size(dest)
        ):
            logger.info("%s: reusing filtered %s", source, dest)
            return Prefiltered(source, dest, identity, source_bytes, out_fs.size(dest), True)

    expr = filter_expression(primary_name)
    work = Path(tempfile.mkdtemp(prefix="gridbuilder-flt-", dir=scratch_dir or _scratch_root()))
    try:
        if isinstance(src_fs, _storage.LocalStorage):
            local_src = Path(source)
        else:
            local_src = work / "source.osm.pbf"
            logger.info("%s: fetching %.1f GB", source, source_bytes / 1e9)
            src_fs.fetch_to_local(source, str(local_src))
        local_out = work / "filtered.osm.pbf"
        # tags-filter reads the input twice to resolve references, so it needs
        # a real file (no stdin); memory is its id sets, a few GB at planet scale.
        subprocess.run(
            ["osmium", "tags-filter", "--overwrite", "--no-progress",
             "-o", str(local_out), str(local_src), expr],
            check=True, capture_output=True, text=True,
        )
        filtered_bytes = local_out.stat().st_size
        logger.info("%s: %s → %.1f MB", source, expr, filtered_bytes / 1e6)
        out_fs.finalize_from_local(str(local_out), dest)
        out_fs.write_text(side_path, json.dumps({
            "source": source,
            "source_identity": identity,
            "source_bytes": source_bytes,
            "primary_name": primary_name,
            "filter": expr,
            "size_bytes": filtered_bytes,
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "osmium": osmium_version(),
            "license": "ODbL 1.0 (OpenStreetMap contributors)",
        }, indent=1, sort_keys=True))
        return Prefiltered(source, dest, identity, source_bytes, filtered_bytes, False)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"osmium tags-filter failed on {source}: {exc.stderr.strip()[-500:]}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _scratch_root() -> str | None:
    root = os.environ.get("FW_GRIDBUILDER_DATA_DIR") or os.environ.get("FW_LOCAL_SCRATCH")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
    return root


def seed_earth_osm(region: str, local_pbf: str, data_dir: str) -> str:
    """Place ``local_pbf`` where earth_osm looks for ``region``'s download.

    earth_osm resolves a region to a Geofabrik URL and, when the file and its
    ``.md5`` are already in ``<data_dir>/pbf/``, verifies the checksum locally
    and makes no request (verified with every proxy pointed at a dead port).
    Writing the md5 of OUR file is what makes that check pass; it states only
    that the file is intact, not that it came from Geofabrik.
    """
    from earth_osm.gfk_data import get_region_tuple

    url = get_region_tuple(region).urls["pbf"]
    name = os.path.basename(url)
    pbf_dir = Path(data_dir) / "pbf"
    pbf_dir.mkdir(parents=True, exist_ok=True)
    dest = pbf_dir / name
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    shutil.copyfile(local_pbf, dest)
    md5 = hashlib.md5()
    with open(dest, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            md5.update(chunk)
    (pbf_dir / (name + ".md5")).write_text(f"{md5.hexdigest()}  {name}\n")
    return str(dest)
