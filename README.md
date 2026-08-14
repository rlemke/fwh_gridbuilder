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

## Maps

`grid.osm.BuildMap` renders the retrieved layers as one self-contained HTML map
(GeoJSON embedded, so it works from `file://`, from a bucket, or from the local
gallery — only basemap tiles come from the network). Beyond upstream parity:
grid-builder ships a map *image* in its README but has no rule that makes one.

```bash
fw ffl run --primary src/gridbuilder/ffl/gridbuilder.ffl \
  --workflow grid.osm.BuildGridMap \
  --inputs '{"countries": ["LU"], "features": ["substation", "line"],
             "out_dir": "s3://afl-cache/gridbuilder/eu",
             "map_dest": "s3://afl-cache/cache/gridbuilder/maps/luxembourg/index.html",
             "title": "Luxembourg power grid (OpenStreetMap)"}'
```

To appear in the local gallery (`fw svc maps`, http://localhost:8090) the map
must follow that server's layout — `<prefix>/<domain>/maps/<name>/index.html`,
prefix `cache/` — which is what `map_dest` above does. Verified: 1,459 features
(832 substations as 107 nodes + 725 areas, 627 lines) served at
`/m/gridbuilder/maps/luxembourg/index.html`.

**Publishing to the public gallery** (rlemke.github.io/facetwork-maps) goes
through `census.Publish.PublishWebBundle`, which needs a `GITHUB_TOKEN`. That
token is not on every host, so a map built here is browsable locally and
published only from a host that holds it.

## Quality and gap audit

`grid.audit.AuditGrid` answers a question the row count cannot: is this extract
usable as a *network*? Upstream lists "validate network model" as a stage; it is
not implemented, and this is that stage at the level that needs no power-flow
solve — graph and geometry over what retrieval already produced, so an audit
costs no downloads and fans out per country.

It reports, ranked worst-first:

| finding | why it matters |
|---|---|
| `dangling_line_end` | a line stops with no other line and no substation nearby — power cannot flow past it |
| `implausible_voltage_transition` | two voltages meet outside a substation: a transformer is missing, or a tag is wrong |
| `grid_island` | lines connected to nothing else — an island, or the extract is missing the connector |
| `missing_voltage` | no usable voltage tag, so the line cannot be assigned a network layer |
| `duplicate_circuit` | same endpoints, near-identical length — double-mapped, or parallel circuits worth confirming |

**On Luxembourg** (627 lines, 832 substations): 283 findings — 79 dangling ends,
117 without voltage, 57 duplicate circuits, 28 small islands, 2 implausible
voltage transitions. 541 of 627 lines (86%) form one connected network. The two
voltage findings cite real ways (110751689 at 65 kV meeting 220 kV) and are
checkable on openstreetmap.org.

Map: https://rlemke.github.io/facetwork-maps/grid/luxembourg-audit/

⚠️ **`tolerance_m` (default 120 m) is the load-bearing parameter.** OSM does not
guarantee a line's endpoint is the same node as the substation it enters, so
"connected" means "within tolerance". Too tight and every junction looks
dangling; too loose and separate assets merge and the islands vanish. The
default under-reports rather than inventing, and every finding records the
tolerance it was judged at.

### Multi-country sweep

Eight countries, chosen small enough to retrieve on a modest connection
(~110 MB of PBF in total):

| country | lines | subs | findings | dangling | no voltage | dup | islands | in main network |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Luxembourg | 627 | 832 | 283 | 79 | 117 | 57 | 32 | 86% |
| Montenegro | 467 | 129 | 416 | 161 | 178 | 29 | 43 | 90% |
| North Macedonia | 316 | 130 | 160 | 28 | 98 | 33 | 2 | 99% |
| Cyprus | 209 | 158 | 100 | 22 | 64 | 8 | 7 | 97% |
| Andorra | 26 | 8 | 21 | 6 | 13 | 0 | 3 | 92% |
| Liechtenstein | 11 | 5 | 19 | 3 | 11 | 3 | 3 | 73% |
| Malta | 7 | 226 | 10 | 5 | 0 | 1 | 5 | 43% |
| Monaco | 0 | 2 | 1 | — | — | — | — | n/a |

Findings per 100 lines ranges from 45 (Luxembourg) to 89 (Montenegro) among the
countries with a real network — so extract quality varies by a factor of two
between neighbours, which is the kind of thing a model built on "whatever OSM
has" inherits silently.

**Two results are about the method, not the data:**

*Malta* shows 226 substations against 7 lines and 43% connectivity. Its
transmission is largely underground, mapped as `power=cable`, and this sweep
retrieved only `power=line`. The grid is not broken; the feature list was
incomplete. Add `cable` to `features` for any country with a significant
underground network before believing its connectivity number.

*Monaco* returned 0 lines and originally audited **clean** — no lines, no
findings, indistinguishable from a healthy grid. That is the silent-success
failure this domain exists to avoid, so an empty extract is now a high-severity
finding in its own right (`no_lines_in_extract`), and it names `power=cable` as
a likely cause.
