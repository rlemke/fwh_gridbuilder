# tools

| tool | does |
|---|---|
| `retrieve_osm.py` / `retrieve-osm.sh` | retrieve OSM power features for one or more countries |
| `compare_with_snakemake.py` | diff this package's output against PyPSA grid-builder's |

Both call `_gridbuilder_tools/`, the same library the FFL handlers use — one
domain, one code path, one cache.

```
countries x features
        |
        v
  retrieve_osm  --earth_osm-->  {country}_{feature}.csv + .geojson + .meta.json
        |
        v
   validate (non-empty)
```
