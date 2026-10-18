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
pytest            # 72 tests, no network and no MongoDB (osmium/tippecanoe ones skip if absent)
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

## The world grid — continents from our own extracts

Upstream retrieves per country and downloads each extract from Geofabrik. Two
things stop that scaling: this fleet cannot reach Geofabrik, and earth_osm
parses every PBF block in pure Python, so its time follows the size of the
INPUT while the output — power infrastructure — is a fraction of a percent of it.

`grid.osm.BuildWorldGridMap` runs per continent instead: `Prefilter`
(`osmium tags-filter nwr/power`) reduces the self-hosted continent extract to
its power objects plus the nodes they reference, `RetrieveFromPbf` runs the
**same earth_osm** on that, and `TileRegion` writes one PMTiles per continent.

**Same data.** Raw and pre-filtered inputs give identical rows, ids and tags
(Luxembourg 1,490 rows, Central America 6,980); coordinates agree to ~1e-13°,
float noise from osmium re-encoding, far below OSM's 1e-7 precision. Central
America: 148 s raw → 3.3 s filtered.

Measured 2026-10-17 (planet split of that day):

| region | extract | filtered | extract time | peak memory | features |
|---|---:|---:|---:|---:|---:|
| Europe | 41.0 GB | 294 MB | 215 s | 7.2 GB | 1,137,147 |
| Asia | 18.6 GB | 143 MB | 178 s | 8.75 GB | 590,960 |
| North America | 21.3 GB | 130 MB | 129 s | 5.5 GB | 430,091 |
| Russia | 4.6 GB | 37 MB | 46 s | 2.3 GB | 174,570 |
| South America | 4.6 GB | 23 MB | 30 s | 1.45 GB | 52,028 |
| Africa | 8.9 GB | 19 MB | 22 s | 1.2 GB | 45,565 |
| Australia-Oceania | 1.7 GB | 23 MB | 17 s | 0.55 GB | 32,557 |
| Central America | 0.9 GB | 2.6 MB | 3.3 s | 0.23 GB | 6,980 |

**The bound is memory per feature (~15 KB), not extract size** — so the
continent is the right unit, and even Europe fits a 10 GB floor. World total,
de-duplicated across continent borders: **1,185,560 lines, 185,876 cables,
884,839 substations.** (Antarctica has no self-hosted extract and is absent.)

⚠️ The filter is disk-bound. osmium makes ~3 passes over the source: Asia took
**83 min on a USB disk and North America 70 s on an internal SSD** — put
`FW_GRIDBUILDER_DATA_DIR` on fast local storage.

**The map.** One page, one PMTiles per continent, read by HTTP range request.
Lines get a tippecanoe `minzoom` from their voltage (≥ 220 kV from z0,
distribution from z8), so zoomed out you see the backbone rather than a random
sample. Files are kept under 95 MB (GitHub's limit is 100) by re-tiling one zoom
shallower: Europe and Asia end at z10, North America z11, the rest z12.

```bash
fw ffl run --primary src/gridbuilder/ffl/gridbuilder.ffl \
  --workflow grid.osm.BuildWorldGridMap \
  --inputs '{"regions": ["north-america","central-america","south-america","europe",
                         "africa","russia","asia","australia-oceania"],
             "out_dir": "s3://afl-cache/gridbuilder/world",
             "map_dir": "s3://afl-cache/cache/gridbuilder/maps/world"}'
```

From a terminal (no runtime): `retrieve_osm.py --countries europe --pbf
s3://osm-extracts/europe-latest.osm.pbf --prefilter --features substation line
cable --out-dir out/`.

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

Eight countries, retrieving **`substation`, `line` and `cable`** — overhead
lines and underground cables are one electrical network, so the audit merges
the conductor layers before analysing connectivity.

| country | conductors | subs | of which transmission | findings | dangling | islands | in main network |
|---|---:|---:|---:|---:|---:|---:|---:|
| Luxembourg | 660 | 832 | 7 | 279 | 70 | 28 | 94% |
| Montenegro | 471 | 129 | 3 | 416 | 160 | 43 | 90% |
| North Macedonia | 318 | 130 | 3 | 115 | 28 | 2 | 99% |
| Cyprus | 218 | 158 | 27 | 100 | 21 | 7 | 97% |
| Andorra | 29 | 8 | 0 | 19 | 5 | 3 | 93% |
| Malta | 23 | 226 | 5 | 11 | 4 | 7 | 57% |
| Liechtenstein | 17 | 5 | 0 | 27 | 5 | 5 | 47% |
| Monaco | 2 | 2 | 0 | 1 | 1 | 1 | 100% |

**Adding `cable` changed the picture where it should.** Luxembourg went from
86% to 94% connected and lost 9 dangling ends — those were overhead lines
meeting underground cables that the line-only audit could not see. Monaco went
from "no lines at all" to a 2-cable network.

**It did not explain Malta, and the earlier README said it would.** That claim
was a hypothesis stated as a cause, and checking it showed otherwise: Malta's
226 substations are **119 `minor_distribution`, 24 `distribution` and just 5
`transmission`**. The mapping effort went into street-level transformer boxes,
not into the network joining them, so cables lifted connectivity only 43% → 57%.
The summary now reports `substation_classes` and `transmission_substations`,
because "226 substations" describes mapping effort rather than the grid.

**Liechtenstein got worse — 73% → 47%.** Adding six cables added a component
that connects to nothing else. That is the honest behaviour: a new layer can
lower connectivity when the new features are themselves disconnected, and at
n=17 conductors these percentages are noise. Treat the small states as
illustrations, not measurements.

Findings per 100 conductors, among countries with a real network: 36
(North Macedonia) to 88 (Montenegro).

⚠️ **This table was re-run on 2026-10-18 after a parser fix**, on the same
2026-08-23 extracts. `parse_voltage` read a FLOAT voltage by its digits, so
`69000.0` became 690 kV. A layer arrives as floats whenever every voltage in it
is a plain number (North Macedonia's lines, Luxembourg's cables) and as strings
when any value is not (`"220000;65000"`), so the bug hit some layers of some
countries: North Macedonia lost 47 findings, Luxembourg 18. The line-only
Luxembourg figures above read string voltages and were not affected.

⚠️ Note what `transmission` count implies: Andorra, Liechtenstein and Monaco
have **zero** substations tagged `substation=transmission`. An audit of those is
an audit of distribution mapping, which is a different question from the one
this domain asks.
