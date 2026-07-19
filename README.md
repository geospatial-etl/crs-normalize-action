# crs-normalize-action

A GitHub Action and command-line tool that finds coordinate reference system problems in spatial
datasets, reprojects what it safely can, and fails the build on what it cannot.

## The problem

CRS bugs do not announce themselves. A shapefile arrives with no `.prj`, a partner sends an extract
in a national grid instead of WGS 84, an export step silently drops the projection metadata, or a
`.prj` sidecar is left behind next to a GeoTIFF that carries its own CRS. Nothing crashes. The
pipeline runs green, the geometry is valid, and the data is quietly in the wrong place — sometimes
by metres, sometimes by continents. It surfaces weeks later as a spatial join that returns nothing,
or a layer that renders in the Gulf of Guinea.

This Action turns those failures into build failures, at the point the data enters the repository.

It deliberately does **not** guess. A dataset with no CRS is a hard failure unless you state the CRS
explicitly, because an incorrect guess produces output that looks entirely valid and is wrong. The
same applies to datum shifts whose grids are not installed: rather than let PROJ fall back to a
coarser transformation, the run fails and tells you which grid to install.

## What it checks

- **Missing CRS** — datasets that declare no coordinate reference system.
- **Mixed CRS** — datasets in the working set that disagree about which CRS they use.
- **Implausible coordinates** — a real geometric check, not a metadata read. Degree-magnitude
  coordinates sitting in a projected CRS, metre-magnitude coordinates labelled as EPSG:4326, and
  data falling outside the declared CRS's area of use.
- **Sidecar disagreement** — a `.prj` file that contradicts the CRS embedded in the dataset, where
  the effective CRS depends on which reader opens the file.
- **Axis order** — a sidecar and an embedded CRS that resolve to the same code but declare opposite
  axis orders, so coordinates read through one path are transposed relative to the other.
- **Unreachable targets** — CRS pairs with no coordinate operation connecting them.
- **Missing datum-shift grids** — where PROJ would silently fall back to a lower-accuracy path.
- **Rotated or south-up rasters** — geotransforms that reprojection cannot honour.
- **Transformation accuracy** — the accuracy PROJ declares for the operation it actually selected,
  compared against a limit you set.

## Quick start

```yaml
name: CRS check

on:
  pull_request:
    paths: ["data/**"]

permissions:
  contents: read
  pull-requests: write

jobs:
  crs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: geospatial-etl/crs-normalize-action@v1
        with:
          paths: data
          target-crs: EPSG:4326
          mode: check
          fail-on: mixed
          comment-on-pr: "true"
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

Problems appear as inline annotations on the offending files, as a job summary table, and as a
single pull request comment that updates in place on each push.

To reproject rather than only report, switch `mode` to `fix` and give it somewhere to write:

```yaml
      - uses: geospatial-etl/crs-normalize-action@v1
        with:
          paths: data
          target-crs: EPSG:3857
          mode: fix
          output-dir: normalized
          resampling: bilinear
```

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `paths` | `.` | Whitespace- or newline-separated files, directories or globs. Directories are walked recursively; recursive globs such as `data/**/*.shp` work. |
| `target-crs` | `""` | CRS every dataset should end up in, for example `EPSG:3857`. Required when `mode` is `fix`. Optional in `check` mode, where supplying it additionally verifies every source CRS can reach it. |
| `assume-crs` | `""` | CRS to attribute to datasets that declare none. Leave empty to treat a missing CRS as unresolvable. |
| `mode` | `check` | `check` inspects and reports without writing. `fix` reprojects resolvable datasets. |
| `fail-on` | `unresolvable` | `unresolvable` fails only on problems the Action cannot fix. `mixed` also fails when datasets merely disagree on CRS. `never` reports without ever failing. |
| `resampling` | `nearest` | Raster resampling: `nearest`, `bilinear`, `cubic`, `cubic_spline`, `lanczos`, `average`, `mode`, `max`, `min`, `med`, `q1`, `q3`. |
| `max-transform-error` | `""` | Fail when the selected coordinate operation declares an accuracy worse than this many metres. |
| `output-dir` | `""` | Write reprojected datasets here, mirroring the input structure, instead of rewriting in place. |
| `comment-on-pr` | `false` | Post a sticky pull request comment. Requires `github-token` with `pull-requests: write`. |
| `summary` | `true` | Write the report to the job summary. |
| `report-path` | `crs-report.json` | Where to write the machine-readable JSON report. |
| `github-token` | `""` | Token used for the pull request comment. |

## Outputs

| Output | Description |
| --- | --- |
| `status` | `clean`, `changed`, `failed` or `error`. |
| `files-scanned` | Number of datasets discovered and inspected. |
| `files-changed` | Number of datasets reprojected. Always `0` in check mode. |
| `crs-histogram` | JSON object mapping each CRS found to the number of datasets using it. |
| `report-path` | Path to the JSON report written by this run. |

## Failure codes

Every finding carries a stable code that appears in the JSON report and as the annotation title.

| Code | Meaning | How to fix it |
| --- | --- | --- |
| `CRS001_MISSING_CRS` | The dataset declares no CRS, so its coordinates cannot be placed on the earth. | Determine the authoring CRS and declare it. Pass `assume-crs` only when you actually know it. |
| `CRS002_MIXED_CRS` | Datasets in the working set use different CRSs. | Reproject the outliers, or run in `fix` mode with a `target-crs`. |
| `CRS003_IMPLAUSIBLE_COORDINATES` | The coordinate magnitudes cannot have come from the declared CRS. | Re-declare the correct CRS. Do not reproject from the wrong one — that compounds the error. |
| `CRS004_SIDECAR_MISMATCH` | A `.prj` sidecar disagrees with the CRS embedded in the dataset. | Delete the stale sidecar or update it to match, so the effective CRS stops depending on the reader. |
| `CRS005_NO_TRANSFORM` | PROJ has no coordinate operation between source and target. | Usually an engineering or unreferenced CRS. Georeference the source, or choose a target sharing a datum path. |
| `CRS006_GRID_UNAVAILABLE` | The best transformation needs a datum-shift grid that is not installed. | Install the grid (`pip install pyproj[network]` with `PROJ_NETWORK=ON`, or fetch it into `PROJ_DATA`). |
| `CRS007_ROTATED_RASTER` | The raster has a rotated or south-up geotransform. | Rectify to a north-up grid first; reprojection would discard the rotation terms. |
| `CRS008_TRANSFORM_ACCURACY` | The selected operation's declared accuracy exceeds `max-transform-error`. | Raise the limit, or install the high-accuracy grid for this datum pair. |
| `CRS009_UNREADABLE` | The dataset could not be opened. | Check the file is complete and its driver is available. Shapefiles need their `.shx` and `.dbf` siblings. |
| `CRS010_AXIS_ORDER` | The declared axis order disagrees with the stored coordinate order. | Rewrite with an explicit authority-compliant CRS so readers stop disagreeing. |
| `CRS011_WRITE_FAILED` | Reprojection succeeded but the output could not be written. | Check permissions and free space. The original is left unmodified. |

## Standalone CLI

The same tool runs outside CI. It is not published to a package index; install it from a clone.

```bash
git clone https://github.com/geospatial-etl/crs-normalize-action.git
cd crs-normalize-action
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
```

Requires Python 3.11 or newer. The geospatial dependencies ship binary wheels bundling GDAL and
PROJ, so no system GDAL install is needed.

Both an executable and a module entry point are available:

```bash
crs-normalize scan data/
python -m crs_normalize scan data/
```

### Command reference

#### `crs-normalize scan PATHS...`

Read-only inspection. Never writes to the datasets.

| Option | Description |
| --- | --- |
| `--target`, `-t` | Target CRS to assess reachability and accuracy against. |
| `--assume-crs` | CRS to attribute to datasets that declare none. |
| `--max-transform-error` | Flag transformations less accurate than this, in metres. |
| `--format`, `-f` | `table` (default), `json` or `github`. |
| `--summary-file` | Append a markdown job summary to this file. |
| `--report-file` | Write the full JSON report to this file. |
| `--fail-on` | `mixed`, `unresolvable` (default) or `never`. |
| `--comment-on-pr` | Post the summary to the pull request. |
| `--verbose`, `-v` | Enable debug logging. |

#### `crs-normalize normalize PATHS... --target CRS`

Reprojects resolvable datasets. Datasets already in the target CRS are left untouched, so a clean
repository produces no diff.

| Option | Description |
| --- | --- |
| `--target`, `-t` | **Required.** Target CRS, for example `EPSG:3857`. |
| `--assume-crs` | CRS to attribute to datasets that declare none. |
| `--output-dir`, `-o` | Write results here, mirroring the input structure, instead of in place. |
| `--resampling` | Raster resampling method. Default `nearest`. |
| `--max-transform-error` | Refuse transformations less accurate than this, in metres. |
| `--check` | Report what would change without writing anything. |
| `--format`, `-f` | `table` (default), `json` or `github`. |
| `--summary-file` | Append a markdown job summary to this file. |
| `--report-file` | Write the full JSON report to this file. |
| `--fail-on` | `mixed`, `unresolvable` (default) or `never`. |
| `--verbose`, `-v` | Enable debug logging. |

### Examples

```bash
# What CRSs are in this repository?
crs-normalize scan data/

# Can everything reach EPSG:3857, and how accurately?
crs-normalize scan data/ --target EPSG:3857 --max-transform-error 1.0

# Machine-readable report for another tool to consume
crs-normalize scan data/ --format json > crs.json

# Dry run: what would reprojecting to British National Grid change?
crs-normalize normalize data/ --target EPSG:27700 --check

# Reproject into a separate tree, interpolating continuous raster data
crs-normalize normalize data/ --target EPSG:4326 -o normalized/ --resampling bilinear

# Only these globs, treating unreferenced files as WGS 84
crs-normalize normalize 'data/**/*.shp' --target EPSG:3857 --assume-crs EPSG:4326
```

### Exit codes

| Code | Name | Meaning |
| --- | --- | --- |
| `0` | clean | No problems found under the active `--fail-on` policy, and nothing needed changing. |
| `1` | failed | At least one blocking problem. Nothing unsafe was written. |
| `2` | changed | Datasets were rewritten, or under `--check` would have been. |
| `3` | usage | The invocation was invalid: an unparseable CRS, an unknown resampling method, a missing target. |

Exit code `2` is what makes `--check` usable as a drift gate:

```bash
crs-normalize normalize data/ --target EPSG:4326 --check
case $? in
  0) echo "Already normalized." ;;
  2) echo "Data would change; run without --check." ; exit 1 ;;
  *) echo "Unresolvable CRS problems." ; exit 1 ;;
esac
```

## Example workflows

Copy-paste workflows live in [`examples/workflows`](examples/workflows):

- [Check on pull request](examples/workflows/check-on-pull-request.yml) — gate PRs, annotate the
  changed files, post a sticky comment.
- [Fix and commit](examples/workflows/fix-and-commit.yml) — scheduled reprojection that opens a pull
  request with the result.
- [Matrix over data directories](examples/workflows/matrix-over-data-dirs.yml) — for repositories
  where different directories legitimately use different projections.

The [self-test workflow](.github/workflows/self-test.yml) runs the Action against fixtures generated
in this repository and asserts its outputs and exit codes.

## How it works

### Detecting the CRS

Vector datasets are read through `pyogrio.read_info`, which parses only the layer header and the
driver's cached extent. Rasters are opened with `rasterio` and only their header is read. Neither
path loads features or pixels, so scanning a large repository is fast.

The full WKT of each CRS is retained alongside its `AUTHORITY:CODE` label. The label is convenient
for histograms and configuration but is lossy — a CRS with no authority code cannot be reconstructed
from it — so all reprojection and transform analysis uses the WKT.

### Judging coordinate plausibility

Rather than hardcoding per-projection coordinate ranges, the tool derives the envelope a CRS can
actually produce: it samples the CRS's declared area of use on a grid, projects those points into
the CRS, and takes the bounds of the result with a tolerance margin. A dataset whose extent falls
outside that envelope is flagged.

Two specific cases are called out explicitly because they dominate real pipelines. Coordinates
confined to the ±180/±90 box inside a projected CRS whose own envelope runs to tens of kilometres
are almost certainly still geographic degrees. Coordinates outside that box in a geographic CRS are
almost certainly projected metres that were mislabelled.

### Assessing transformations

Transform availability is not assumed from the CRS pair. For each distinct source CRS the tool
builds a `pyproj.transformer.TransformerGroup` to the target, which exposes both the operation PROJ
would select and the operations it could not use:

- An empty group means the two CRSs are not connected at all — reported as `CRS005_NO_TRANSFORM`.
- `best_available` being false means PROJ's preferred operation needs a datum-shift grid that is not
  installed and it would fall back to a coarser one — reported as `CRS006_GRID_UNAVAILABLE`, naming
  the missing grid.
- The selected operation's `description` and declared `accuracy` are recorded for every dataset and
  shown in the job summary, so the error budget of a run is visible rather than implicit.

### Writing safely

Reprojected datasets are written to a temporary sibling and moved into place atomically, so an
interrupted run cannot leave a half-written dataset where the original was. Datasets carrying a
blocking finding are never rewritten, and shapefile sidecars are carried across with their dataset.

## Limitations

- **Vector CRS is per dataset, not per feature.** Formats that permit per-geometry CRS are treated
  as having the layer's declared CRS.
- **Extent-based plausibility needs an area of use.** Custom CRSs that declare none are not
  plausibility-checked; their transform availability and accuracy still are.
- **Rotated rasters are reported, not rectified.** Rectifying is a lossy resampling decision that
  belongs to you, not to a CI gate.
- **Vertical CRSs and 3D transformations are not handled.** Only the horizontal component is
  considered; height values pass through untouched.
- **No network by default.** PROJ will not download datum-shift grids unless you enable
  `PROJ_NETWORK`, which is why a missing grid is a failure rather than a silent fallback.
- **In-place raster reprojection rewrites the whole file.** Large rasters are fully re-encoded; the
  source profile (compression, tiling, nodata) is preserved but block layout may change.
- **GeoPackages with multiple layers** are read as their first layer.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Tests build synthetic GeoPackages and GeoTIFFs on the fly, so there are no binary fixtures in the
repository.

## Further reading

Background on the problems this Action gates against:
[normalizing CRS across mixed datasets](https://www.geospatial-etl.com/automated-vector-raster-cleaning-workflows/crs-normalization-across-mixed-datasets/)
covers why guessing a missing CRS is worse than failing, and
[raster alignment and resampling](https://www.geospatial-etl.com/automated-vector-raster-cleaning-workflows/raster-alignment-resampling-techniques/)
explains why the resampling method matters when warping categorical versus continuous data. For
fitting checks like this into a wider pipeline, see
[orchestrating spatial ETL pipelines](https://www.geospatial-etl.com/orchestrating-spatial-etl-pipelines/).

## License

MIT. See [LICENSE](LICENSE).

---

Maintained by [Geospatial ETL](https://www.geospatial-etl.com/).
