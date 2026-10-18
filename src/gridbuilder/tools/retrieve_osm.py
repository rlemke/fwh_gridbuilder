#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Retrieve OSM power infrastructure for one or more countries.

The terminal surface of the same code the FFL handlers run — one domain, one
code path, one cache.

Usage:
  retrieve_osm.py --countries BE --features substation line --out-dir out/
  retrieve_osm.py --countries BE LU --source overpass --force
  retrieve_osm.py --countries BE --use-mock --out-dir /tmp/x   # offline
  retrieve_osm.py --countries europe --pbf s3://osm-extracts/europe-latest.osm.pbf \
      --prefilter --features substation line cable --out-dir out/   # no Geofabrik

stdout: one JSON object per extraction (pipeable).
stderr: progress and errors.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gridbuilder_tools import prefilter, retrieve  # noqa: E402

logger = logging.getLogger("retrieve_osm")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--countries", nargs="+", required=True, help="ISO codes or names (BE, benin)")
    ap.add_argument("--features", nargs="+", default=list(retrieve.DEFAULT_FEATURES))
    ap.add_argument("--primary-name", default=retrieve.DEFAULT_PRIMARY)
    ap.add_argument("--source", default="geofabrik", choices=list(retrieve.VALID_SOURCES))
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--force", action="store_true",
                    help="re-extract even if the cache is current (also re-downloads the PBF)")
    ap.add_argument("--no-mp", action="store_true", help="disable earth-osm multiprocessing")
    ap.add_argument("--no-stream", action="store_true", help="disable the streaming backend")
    ap.add_argument("--cache-primary", action="store_true", help="cache primary feature data")
    ap.add_argument("--target-date", default=None, help="historical snapshot, ISO 8601")
    ap.add_argument("--use-mock", action="store_true", help="offline deterministic extracts")
    ap.add_argument("--pbf", default=None,
                    help="extract from this PBF (path or s3://) instead of downloading; "
                         "--countries then names the earth_osm region it stands for")
    ap.add_argument("--prefilter", action="store_true",
                    help="with --pbf: osmium-filter it to the primary key first (same rows, far faster)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level, stream=sys.stderr, format="%(levelname)s %(message)s")

    if args.pbf and len(args.countries) != 1:
        ap.error("--pbf stands for exactly one region; pass one --countries value")
    pbf = args.pbf
    if pbf and args.prefilter:
        pbf = prefilter.prefilter(pbf, retrieve._storage.Storage.join(args.out_dir, "_prefiltered"),
                                  primary_name=args.primary_name, force=args.force).filtered

    failures = 0
    for country in args.countries:
        for feature in args.features:
            try:
                result = retrieve.retrieve_feature(
                    country,
                    feature,
                    args.out_dir,
                    primary_name=args.primary_name,
                    source=args.source,
                    force=args.force,
                    mp=not args.no_mp,
                    stream_backend=not args.no_stream,
                    cache_primary=args.cache_primary,
                    target_date=args.target_date,
                    use_mock=args.use_mock,
                    pbf=pbf,
                )
            except Exception as exc:  # noqa: BLE001 — one bad pair must not stop the rest
                logger.error("%s/%s failed: %s", country, feature, exc)
                failures += 1
                continue
            print(json.dumps(result.as_dict()))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
