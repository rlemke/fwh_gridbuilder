# fwh_gridbuilder

Power-grid models from OpenStreetMap, as an FFL workflow — the Facetwork
equivalent of [PyPSA `grid-builder`](https://github.com/PyPSA/grid-builder), a
Snakemake workflow that does the same job.

It calls the **same extractor** (`earth_osm`) upstream calls. Only the
orchestrator differs, which is the point: a comparison that also swaps the
payload compares nothing.

## FFL at a glance

```ffl
workflow BuildGrid(countries: Json, features: Json, out_dir: String, concurrency: Int = 2)
    => (sets: Json, countries_done: Int)
    andThen foreach c in $.countries limit $.concurrency {
        country = RetrieveCountry(country = $.c, features = $.features, out_dir = $.out_dir, …)
        yield BuildGrid(sets = country.sets, countries_done = 1)
    }
```

Two nested `foreach` levels are the two `expand()` dimensions upstream fans out
over (countries × features). More examples: [docs/ffl-examples.md](docs/ffl-examples.md).

## Scope — what is actually ported

Upstream's README describes four stages (retrieve → clean/infer → topology →
validate). As of commit `2f20a35` **only retrieve is implemented**: `rule
retrieve_osm` and the `rule retrieve_osm_all` aggregate. This package ports
that, and claims nothing about the three stages neither project has built.

`ValidateRetrieval` is deliberately **beyond** upstream parity and marked as
such in the FFL.

## Verified equivalence

Both workflows were run on the same input (`LU`, `power=substation`, Geofabrik)
and the outputs compared with [`tools/compare_with_snakemake.py`](tools/compare_with_snakemake.py):

```
columns:  same (12)
rows:     snakemake 832, facetwork 832
row data: 0 differing (after normalising tag-key order)
bytes:    differ (earth_osm tag-key order is unstable)
RESULT: same data
```

The byte difference is worth stating plainly because it is **not** a difference
between the orchestrators: `earth_osm` does not serialise its nested
`other_tags` dict in a stable key order, so two runs of the *same* workflow
differ byte-wise too. The consequence applies to both projects equally — neither
can use a content hash of these CSVs as a cache key.

## What differs, and why it matters

| | grid-builder (Snakemake) | this package (FFL) |
|---|---|---|
| **Fan-out** | `expand()` over countries × features, resolved at DAG-construction time on the submitting machine | two nested `foreach` blocks over values that arrive at run time |
| **Bounding it** | `--cores`, global | `limit` on the country loop — each iteration downloads a regional PBF onto a shared disk |
| **Reuse decision** | outputs exist and are newer than inputs. `rule retrieve_osm` declares **no file inputs**, so nothing can ever make its outputs look stale | recorded provenance: extractor version, tool version, source, country/feature/primary, target date, size |
| **Escape hatch** | `force_redownload: true`, set by a human who already suspected | `force`, plus automatic re-derivation when any recorded input moves |
| **Where outputs live** | local filesystem paths | `out_dir` goes through a storage backend — a local path or `s3://`, unchanged. Verified against MinIO |
| **Completion evidence** | the output file | a persisted step state machine, surviving the machine that started the run |

The reuse row is the substantive one. Upstream's retrieve rule has no inputs, so
after the first successful run its outputs are permanently "up to date" —
re-running a year later reports nothing to do and serves year-old OSM data.
Here the same situation re-derives, because `earth-osm 3.0.2` no longer matches
the `3.0.1` recorded in the sidecar.

That is not a claim that this design is free: it is strictly more bookkeeping,
and it is only as good as the keys the author chose to record. See the thesis'
§13.3 for the argument and its costs.

## Install

```bash
pip install -e .            # pulls earth-osm
pip install -e '.[s3]'      # + boto3, for s3:// output
fw runner start --domain gridbuilder
```

## Use

From the terminal (the same code the handlers run):

```bash
python src/gridbuilder/tools/retrieve_osm.py --countries LU --features substation --out-dir out/
python src/gridbuilder/tools/retrieve_osm.py --countries BE --use-mock --out-dir /tmp/x   # offline
```

As a workflow:

```bash
fw ffl run --primary src/gridbuilder/ffl/gridbuilder.ffl \
    --workflow grid.osm.BuildGridChecked \
    --inputs '{"countries": ["LU"], "features": ["substation", "line"], "out_dir": "out"}'
```

## Reproducing the comparison

```bash
git clone https://github.com/PyPSA/grid-builder && cd grid-builder
# upstream pins software-deployment-method: conda in workflow/profiles/default/
snakemake -s workflow/Snakefile --configfile <(echo 'countries: [LU]') --cores 2

python tools/compare_with_snakemake.py \
    --snakemake-dir grid-builder/resources/osm/out \
    --facetwork-dir out/ --country LU --feature substation
```

## Tests

```bash
pytest            # 25 tests, no network and no MongoDB (use_mock)
```

## Licence

Apache-2.0. Upstream `grid-builder` is MIT; OpenStreetMap data is
[ODbL](https://opendatacommons.org/licenses/odbl/).
