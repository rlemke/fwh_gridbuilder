# FFL examples — gridbuilder

Complete, compile-checked scenarios against this domain's own facets. Every
fenced ```ffl block below compiles with:

```bash
fw ffl compile --check --primary <block> --library src/gridbuilder/ffl/gridbuilder.ffl
```

The facets: `grid.osm.Retrieve` (one country × one feature),
`grid.osm.ValidateRetrieval` (non-empty check), and the composed
`grid.osm.RetrieveCountry` / `grid.osm.BuildGrid` / `grid.osm.BuildGridChecked`.

---

## 1. The minimal workflow — one country, one feature

```ffl
namespace demo.one {
    use grid.osm

    workflow OneCountry(country: String, out_dir: String) => (path: String, elements: Int)
        andThen {
            got = grid.osm.Retrieve(country = $.country, feature = "substation", out_dir = $.out_dir)
            yield OneCountry(path = got.result.csv_path, elements = got.result.elements)
        }
}
```

`got.result` is the `FeatureSet` schema, so its fields are read through it.

---

## 2. Fan out over features — the inner `expand()`

```ffl
namespace demo.features {
    use grid.osm

    facet EachFeature(country: String, features: Json, out_dir: String) => (paths: Json)
        andThen foreach f in $.features {
            got = grid.osm.Retrieve(country = $.country, feature = $.f, out_dir = $.out_dir)
            // Brackets aggregate; a bare value would leave only the LAST feature.
            yield EachFeature(paths = [got.result.csv_path])
        }

    workflow Belgium(out_dir: String) => (paths: Json) andThen {
        all = EachFeature(country = "BE", features = ["substation", "line"], out_dir = $.out_dir)
        yield Belgium(paths = all.paths)
    }
}
```

**The brackets are the thing to copy.** `yield F(xs = v)` assigns, so each
iteration overwrites the previous and the result is the final iteration's value
alone — while every iteration's step still ran and the workflow still reports
success. `yield F(xs = [v])` concatenates.

---

## 3. Bounding the fan-out

```ffl
namespace demo.bounded {
    use grid.osm

    workflow Europe(countries: Json, out_dir: String, width: Int = 3) => (done: Int)
        andThen foreach c in $.countries limit $.width {
            got = grid.osm.Retrieve(country = $.c, feature = "line", out_dir = $.out_dir)
            yield Europe(done = 1)
        }
}
```

`limit` is right here for a concrete reason, not as a general precaution: each
iteration downloads a regional PBF (Belgium is ~660 MB) into shared scratch and
pulls from one origin. Passing it as a parameter (`width: Int = 3`) lets one
workflow serve a laptop and a fleet; `0` means unbounded.

---

## 4. Refusing an empty extract

```ffl
namespace demo.checked {
    use grid.osm

    workflow Checked(country: String, out_dir: String) => (elements: Int, detail: String)
        andThen {
            got = grid.osm.Retrieve(country = $.country, feature = "substation", out_dir = $.out_dir)
            checked = grid.osm.ValidateRetrieval(csv_path = got.result.csv_path, min_elements = 10)
            sys.assert(checked.ok == true)
            yield Checked(elements = checked.elements, detail = checked.detail)
        }
}
```

A download that succeeds and yields a header-only CSV is this domain's quietest
failure: downstream it becomes a grid with no substations, which reads as a
modelling result rather than a missing file.

---

## 5. Branching on whether the data actually changed

```ffl
namespace demo.gated {
    use grid.osm

    event facet BuildTopology(csv_path: String) => (nodes: Int)

    workflow Refresh(country: String, out_dir: String) => (nodes: Int) andThen {
        got = grid.osm.Retrieve(
            country = $.country, feature = "substation", out_dir = $.out_dir
        ) andThen when {
            case $.result.was_cached == false => {
                built = BuildTopology(csv_path = $.result.csv_path)
                yield Refresh(nodes = built.nodes)
            }
            case _ => {
                yield Refresh(nodes = 0)
            }
        }
    }
}
```

`was_cached` is the retriever's own answer to "did this change?", established
from recorded provenance rather than inferred from a timestamp — so downstream
work can be gated on it.

---

## 6. Historical snapshots

```ffl
namespace demo.historical {
    use grid.osm

    workflow AsOf(country: String, out_dir: String, when: String) => (path: String)
        andThen {
            got = grid.osm.Retrieve(
                country = $.country,
                feature = "substation",
                out_dir = $.out_dir,
                target_date = $.when
            )
            yield AsOf(path = got.result.csv_path)
        }
}
```

`target_date` (ISO 8601) is part of the cache identity, not a fetch option: a
different snapshot is different data, so changing it re-derives rather than
serving today's extract under last year's name.

---

## 7. Composing with the built-ins

```ffl
namespace demo.composed {
    use grid.osm
    use fw.file

    workflow WithManifest(country: String, out_dir: String) => (manifest: String) andThen {
        got = grid.osm.Retrieve(country = $.country, feature = "line", out_dir = $.out_dir)
        listed = fw.file.List(path = $.out_dir, pattern = "*.csv")
        written = fw.file.WriteJson(
            path = $.out_dir ++ "/inventory.json", data = listed.paths, indent = 2
        )
        yield WithManifest(manifest = written.path)
    }
}
```

`fw.file.*` ships with the framework — no handler, and no `--library`.

---

## Cheat sheet

| Want | Write |
|---|---|
| one extraction | `grid.osm.Retrieve(country =, feature =, out_dir =)` |
| a schema field | `got.result.csv_path` |
| fan out | `andThen foreach f in $.features { … }` |
| **aggregate a fan-out** | `yield F(xs = [item])` — brackets, or you get the last one |
| bound the width | `foreach c in $.countries limit $.width` |
| force a refresh | `force = true` (also re-downloads the PBF) |
| run offline | `use_mock = true` |
| gate on change | `andThen when { case $.result.was_cached == false => … }` |
