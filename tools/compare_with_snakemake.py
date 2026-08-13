#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare this package's output against PyPSA grid-builder's, file for file.

Usage:
  compare_with_snakemake.py --snakemake-dir <grid-builder>/resources/osm/out \
                            --facetwork-dir out/ --country LU --feature substation

The equivalence claim in the README rests on this, so it is a script rather than
a paragraph. It reports three things, in increasing strictness:

  1. the same files exist, with the same columns and the same ids;
  2. every row is equal once nested-tag key ORDER is normalised;
  3. whether the bytes are identical.

(2) is the real question — "did the two orchestrators produce the same data?" —
and (3) is reported separately because the answer is no, for a reason that has
nothing to do with either orchestrator: `earth_osm` does not serialise its
nested `other_tags` dict in a stable key order, so two runs of the SAME workflow
differ byte-wise too. That is worth knowing rather than hiding, because it means
neither project can use a content hash of these files as a cache key.

Exit code 0 when the data matches, 1 when it does not.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

NESTED_TAG_COLUMN = "other_tags"


def load(path: Path) -> tuple[list[dict], list[str]]:
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader), list(reader.fieldnames or [])


def normalise(row: dict) -> dict:
    """Row with the nested tag blob's keys sorted, so ORDER stops mattering."""
    out = dict(row)
    blob = out.get(NESTED_TAG_COLUMN)
    if blob:
        try:
            out[NESTED_TAG_COLUMN] = json.dumps(json.loads(blob), sort_keys=True)
        except ValueError:
            pass  # not JSON — compare verbatim
    return out


def compare(sm_csv: Path, fw_csv: Path) -> int:
    for path, label in ((sm_csv, "snakemake"), (fw_csv, "facetwork")):
        if not path.is_file():
            print(f"MISSING ({label}): {path}")
            return 1

    sm_rows, sm_cols = load(sm_csv)
    fw_rows, fw_cols = load(fw_csv)

    print(f"columns:  {'same' if sm_cols == fw_cols else 'DIFFER'} ({len(sm_cols)})")
    if sm_cols != fw_cols:
        print(f"  only in snakemake: {sorted(set(sm_cols) - set(fw_cols))}")
        print(f"  only in facetwork: {sorted(set(fw_cols) - set(sm_cols))}")
        return 1

    sm = {r["id"]: r for r in sm_rows}
    fw = {r["id"]: r for r in fw_rows}
    print(f"rows:     snakemake {len(sm)}, facetwork {len(fw)}")
    if set(sm) != set(fw):
        print(f"  only in snakemake: {sorted(set(sm) - set(fw))[:5]}")
        print(f"  only in facetwork: {sorted(set(fw) - set(sm))[:5]}")
        return 1

    differing = [i for i in sm if normalise(sm[i]) != normalise(fw[i])]
    print(f"row data: {len(differing)} differing (after normalising tag-key order)")
    for osm_id in differing[:3]:
        for col in sm_cols:
            if sm[osm_id].get(col) != fw[osm_id].get(col):
                print(f"  id={osm_id} col={col}")
                print(f"    snakemake: {str(sm[osm_id][col])[:100]}")
                print(f"    facetwork: {str(fw[osm_id][col])[:100]}")

    identical = sm_csv.read_bytes() == fw_csv.read_bytes()
    print(f"bytes:    {'identical' if identical else 'differ (earth_osm tag-key order is unstable)'}")
    return 1 if differing else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snakemake-dir", required=True, type=Path)
    ap.add_argument("--facetwork-dir", required=True, type=Path)
    ap.add_argument("--country", required=True)
    ap.add_argument("--feature", required=True)
    args = ap.parse_args()

    name = f"{args.country}_{args.feature}.csv"
    rc = compare(args.snakemake_dir / name, args.facetwork_dir / name)
    print("\nRESULT:", "same data" if rc == 0 else "DIFFERENT")
    return rc


if __name__ == "__main__":
    sys.exit(main())
