#!/usr/bin/env bash
#
# Docker entrypoint for the crs-normalize GitHub Action.
#
# Arguments arrive positionally in the order declared by action.yml. Empty
# strings mean "input not supplied" because GitHub always passes every arg.
#
# The CLI's exit code is preserved and re-raised at the end, after outputs and
# the job summary have been written, so a failing check still reports fully.

set -uo pipefail

PATHS_INPUT="${1:-.}"
TARGET_CRS="${2:-}"
ASSUME_CRS="${3:-}"
MODE="${4:-check}"
FAIL_ON="${5:-unresolvable}"
RESAMPLING="${6:-nearest}"
MAX_TRANSFORM_ERROR="${7:-}"
OUTPUT_DIR="${8:-}"
COMMENT_ON_PR="${9:-false}"
SUMMARY="${10:-true}"
REPORT_PATH="${11:-crs-report.json}"

die() {
  echo "::error title=crs-normalize::$1"
  exit 3
}

case "${MODE}" in
  check | fix) ;;
  *) die "mode must be 'check' or 'fix', got '${MODE}'." ;;
esac

case "${FAIL_ON}" in
  mixed | unresolvable | never) ;;
  *) die "fail-on must be 'mixed', 'unresolvable' or 'never', got '${FAIL_ON}'." ;;
esac

if [[ "${MODE}" == "fix" && -z "${TARGET_CRS}" ]]; then
  die "target-crs is required when mode is 'fix'. Set it to the CRS you want every dataset normalized to, for example 'EPSG:3857'."
fi

# Split the paths input on whitespace and newlines into an argv array, so that
# globs reach the CLI unexpanded and are resolved there consistently.
read -r -a PATH_ARGS <<<"$(echo "${PATHS_INPUT}" | tr '\n' ' ')"
if [[ ${#PATH_ARGS[@]} -eq 0 ]]; then
  PATH_ARGS=(".")
fi

ARGS=()
if [[ "${MODE}" == "fix" ]]; then
  ARGS+=("normalize" "${PATH_ARGS[@]}" "--target" "${TARGET_CRS}" "--resampling" "${RESAMPLING}")
  [[ -n "${OUTPUT_DIR}" ]] && ARGS+=("--output-dir" "${OUTPUT_DIR}")
else
  ARGS+=("scan" "${PATH_ARGS[@]}")
  [[ -n "${TARGET_CRS}" ]] && ARGS+=("--target" "${TARGET_CRS}")
fi

[[ -n "${ASSUME_CRS}" ]] && ARGS+=("--assume-crs" "${ASSUME_CRS}")
[[ -n "${MAX_TRANSFORM_ERROR}" ]] && ARGS+=("--max-transform-error" "${MAX_TRANSFORM_ERROR}")

ARGS+=("--fail-on" "${FAIL_ON}")
ARGS+=("--format" "github")
ARGS+=("--report-file" "${REPORT_PATH}")

if [[ "${SUMMARY}" == "true" && -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  ARGS+=("--summary-file" "${GITHUB_STEP_SUMMARY}")
fi

if [[ "${COMMENT_ON_PR}" == "true" ]]; then
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    ARGS+=("--comment-on-pr")
  else
    echo "::warning title=crs-normalize::comment-on-pr is enabled but no github-token was supplied, so no comment will be posted."
  fi
fi

crs-normalize "${ARGS[@]}"
EXIT_CODE=$?

# Translate the report into Action outputs. This must not mask the CLI's
# verdict, so its own failure is reported but does not change EXIT_CODE.
python -m crs_normalize.action --report "${REPORT_PATH}" --exit-code "${EXIT_CODE}" ||
  echo "::warning title=crs-normalize::Could not write Action outputs."

exit "${EXIT_CODE}"
