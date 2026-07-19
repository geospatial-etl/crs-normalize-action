#!/usr/bin/env bash
#
# End-to-end Docker test for the crs-normalize GitHub Action.
#
# Builds the image and drives it the way the GitHub Actions runner does:
# eleven positional args in action.yml order, the workspace bind-mounted at
# /github/workspace, and GITHUB_OUTPUT / GITHUB_STEP_SUMMARY pointing at files
# the harness reads back. Asserts exit codes and Action outputs.
#
# Usage:  ./docker-smoke-test.sh [path-to-repo]
#
# Set DOCKER to run the client differently, e.g. when the invoking user is not
# in the 'docker' group:
#
#   DOCKER="sudo docker" ./docker-smoke-test.sh
#   DOCKER="sg docker -c docker" ./docker-smoke-test.sh

set -uo pipefail

DOCKER="${DOCKER:-docker}"
REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON="${PYTHON:-${REPO}/.venv/bin/python}"
IMAGE="crs-normalize-action:smoke"
# The workspace must live on a path the Docker daemon can actually see. A
# containerised or sandboxed /tmp is a private overlay: the bind mount then
# resolves to an empty directory on the host and every scan finds nothing, so
# default to a directory beside the repo and allow an override.
WORK_ROOT="${WORK_ROOT:-${REPO}/.docker-smoke}"
mkdir -p "${WORK_ROOT}"
WORK="$(mktemp -d "${WORK_ROOT}/wsXXXXXX")"
PASS=0
FAIL=0

cleanup() {
  # Any root-owned leftovers from a failed run cannot be removed directly.
  rm -rf "${WORK}" 2>/dev/null ||
    ${DOCKER} run --rm -v "${WORK}:/w" alpine sh -c 'rm -rf /w/..?* /w/.[!.]* /w/*' 2>/dev/null || true
  rmdir "${WORK}" 2>/dev/null || true
}
trap cleanup EXIT

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

# --- 1. Build ---------------------------------------------------------------
say "Building image"
${DOCKER} build -t "${IMAGE}" "${REPO}" || { echo "BUILD FAILED"; exit 1; }
echo "image size: $(${DOCKER} image inspect "${IMAGE}" --format '{{.Size}}' | numfmt --to=iec)"

# --- 2. Fixtures ------------------------------------------------------------
# Built with the repo's own venv so the harness reuses the test helpers rather
# than re-deriving how to write a CRS-tagged dataset.
say "Generating fixtures"
mkdir -p "${WORK}/data"
REPO="${REPO}" "${PYTHON}" - "$WORK" <<'PY'
import os, sys, pathlib
sys.path.insert(0, os.environ["REPO"])
from shapely.geometry import Point
from tests.conftest import write_vector, write_raster

d = pathlib.Path(sys.argv[1]) / "data"
write_vector(d / "points_4326.gpkg", "EPSG:4326")
# Real British National Grid eastings/northings. The default fixture points are
# degrees, which would additionally trip the implausible-coordinate check and
# muddle "mixed CRS" with a second, unrelated finding.
write_vector(d / "points_27700.gpkg", "EPSG:27700",
             [Point(530000.0, 180000.0), Point(531000.0, 181000.0)])
write_vector(d / "points_nocrs.gpkg", None)           # unresolvable
write_raster(d / "grid_4326.tif", "EPSG:4326")
print("fixtures:", *(p.name for p in sorted(d.iterdir())))
PY

# --- 3. Runner harness ------------------------------------------------------
# args: paths target assume mode fail-on resampling max-err out-dir pr summary report
run_action() {
  local expect="$1"; shift
  local desc="$1"; shift
  : >"${WORK}/gh_output"
  : >"${WORK}/gh_summary"
  local out rc
  out=$(${DOCKER} run --rm \
    -v "${WORK}:/github/workspace" \
    -w /github/workspace \
    -e GITHUB_OUTPUT=/github/workspace/gh_output \
    -e GITHUB_STEP_SUMMARY=/github/workspace/gh_summary \
    "${IMAGE}" "$@" 2>&1)
  rc=$?
  if [[ "${rc}" == "${expect}" ]]; then
    printf '  \033[32mPASS\033[0m %-46s exit=%s\n' "${desc}" "${rc}"; PASS=$((PASS+1))
  else
    printf '  \033[31mFAIL\033[0m %-46s exit=%s (want %s)\n' "${desc}" "${rc}" "${expect}"; FAIL=$((FAIL+1))
    echo "${out}" | sed 's/^/       | /'
  fi
  LAST_OUT="${out}"
}

say "Exit-code matrix"
# fail-on=never over a clean single-CRS file -> 0
run_action 0 "clean scan, fail-on=never" \
  "data/points_4326.gpkg" "" "" "check" "never" "nearest" "" "" "false" "true" "crs-report.json"

# mixed CRS present, fail-on=mixed -> 1
run_action 1 "mixed CRS, fail-on=mixed" \
  "data/*.gpkg" "" "" "check" "mixed" "nearest" "" "" "false" "true" "crs-report.json"

# missing CRS with no assume-crs -> unresolvable -> 1
run_action 1 "missing CRS, unresolvable" \
  "data/points_nocrs.gpkg" "EPSG:3857" "" "check" "unresolvable" "nearest" "" "" "false" "true" "crs-report.json"

# missing CRS but assume-crs supplied -> resolvable -> 0
run_action 0 "missing CRS rescued by assume-crs" \
  "data/points_nocrs.gpkg" "EPSG:3857" "EPSG:4326" "check" "unresolvable" "nearest" "" "" "false" "true" "crs-report.json"

# bad mode -> usage error -> 3
run_action 3 "invalid mode rejected" \
  "data" "" "" "banana" "never" "nearest" "" "" "false" "true" "crs-report.json"

# fix mode without target-crs -> usage error -> 3
run_action 3 "fix without target-crs rejected" \
  "data" "" "" "fix" "never" "nearest" "" "" "false" "true" "crs-report.json"

# fix mode writing to an output dir -> changes made -> 2
run_action 2 "fix mode reprojects (changed)" \
  "data/points_4326.gpkg data/grid_4326.tif" "EPSG:3857" "" "fix" "never" "nearest" "" "out" "false" "true" "crs-report.json"

# --- 4. Contract checks -----------------------------------------------------
say "Action outputs and side effects"
check() {
  if eval "$2"; then printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1))
  else printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); fi
}
check "GITHUB_OUTPUT has status="        "grep -q '^status=' '${WORK}/gh_output'"
check "GITHUB_OUTPUT has files-scanned=" "grep -q '^files-scanned=' '${WORK}/gh_output'"
check "GITHUB_OUTPUT has files-changed=" "grep -q '^files-changed=' '${WORK}/gh_output'"
check "GITHUB_OUTPUT has crs-histogram=" "grep -q '^crs-histogram=' '${WORK}/gh_output'"
check "step summary written"             "[ -s '${WORK}/gh_summary' ]"
check "JSON report written"              "[ -s '${WORK}/crs-report.json' ]"
check "report is valid JSON"             "python3 -m json.tool '${WORK}/crs-report.json' >/dev/null"
check "fix wrote to output dir"          "[ -d '${WORK}/out' ] && [ -n \"\$(ls -A '${WORK}/out')\" ]"
check "fix left inputs untouched"        "[ -f '${WORK}/data/points_4326.gpkg' ]"

# A Docker action runs as root while later workflow steps run as the runner
# user; root-owned outputs break the documented fix-and-commit flow.
OWNER="$(stat -c '%U' "${WORK}")"
check "outputs not left root-owned"      "[ -z \"\$(find '${WORK}/out' ! -user '${OWNER}' -print -quit)\" ]"
check "report not left root-owned"       "[ \"\$(stat -c '%U' '${WORK}/crs-report.json')\" = '${OWNER}' ]"

say "Reprojected output really is EPSG:3857"
REPO="${REPO}" "${PYTHON}" - "$WORK" <<'PY' && PASS=$((PASS+1)) || FAIL=$((FAIL+1))
import sys, pathlib, geopandas as gpd
out = pathlib.Path(sys.argv[1]) / "out"
hits = list(out.rglob("*.gpkg"))
assert hits, f"no vector output under {out}"
crs = gpd.read_file(hits[0]).crs
assert crs and crs.to_epsg() == 3857, f"expected EPSG:3857, got {crs}"
print(f"  PASS  {hits[0].name} -> {crs.to_epsg()}")
PY

say "Result"
echo "  passed: ${PASS}   failed: ${FAIL}"
[[ "${FAIL}" -eq 0 ]] || exit 1
