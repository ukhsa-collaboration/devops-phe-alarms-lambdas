#!/usr/bin/env bash

set -euo pipefail

# We're calling this script from the lambdas directory
# shellcheck disable=SC1091
source ../.github/workflows/scripts/functions.sh

normalise_path() {
  local raw=$1
  # Strip any leading "./" to keep comparisons simple.
  printf '%s' "${raw#./}"
}

select_tests_dir() {
  local lambda_dir=$1
  local candidate
  local tests_dir=""

  # Allow multiple candidates separated by ":" while maintaining a sensible default.
  IFS=":" read -r -a candidates <<<"${RELATIVE_UNIT_TESTS_DIR:-tests/unit}"
  candidates+=("tests") # Fallback for older layouts.

  for candidate in "${candidates[@]}"; do
    candidate="$(normalise_path "${candidate}")"
    if [[ -d "${lambda_dir}/${candidate}" ]]; then
      tests_dir="${candidate}"
      break
    fi
  done

  printf '%s' "${tests_dir}"
}

run_tests_for_lambda() {
  local lambda_dir=$1
  local dir_name
  dir_name="$(basename "${lambda_dir}")"

  local tests_subdir
  tests_subdir="$(select_tests_dir "${lambda_dir}")"

  if [[ -z "${tests_subdir}" ]]; then
    warn "Skipping '${dir_name}' – no unit tests found"
    return
  fi

  info "Running unit tests for '${dir_name}' (path: ${tests_subdir})"

  (
    set -euo pipefail

    cd "${lambda_dir}"

    # Ensure the parent 'lambdas/' directory stays on sys.path so package imports work.
    local project_root
    project_root="$(dirname "${PWD}")"
    export PYTHONPATH="${project_root}${PYTHONPATH:+:${PYTHONPATH}}"

    local temp_env
    temp_env="$(mktemp -d "${TMPDIR:-/tmp}/unit-tests-${dir_name}-XXXXXX")"
    trap 'rm -rf "${temp_env}"' EXIT

    local python_version="${PYTHON_VERSION:-3.12}"

    uv venv --seed --python "${python_version}" "${temp_env}"
    # shellcheck disable=SC1091
    source "${temp_env}/bin/activate"

    local cache_args=()
    if [[ -n "${PIP_CACHE_DIR:-}" ]]; then
      cache_args+=(--cache-dir "${PIP_CACHE_DIR}")
    fi

    if [[ -f "pyproject.toml" ]]; then
      uv pip install "${cache_args[@]}" --editable ".[dev]"
    elif [[ -f "requirements.txt" ]]; then
      uv pip install "${cache_args[@]}" -r "requirements.txt"
      uv pip install "${cache_args[@]}" pytest
      if [[ -f "requirements-dev.txt" ]]; then
        uv pip install "${cache_args[@]}" -r "requirements-dev.txt"
      fi
    else
      warn "No dependency metadata found for '${dir_name}'; installing pytest only"
      uv pip install "${cache_args[@]}" pytest
    fi

    pytest -vv "${tests_subdir}"
  )
}

main() {
  require_cmd uv mktemp

  if [[ -z "${RELATIVE_UNIT_TESTS_DIR:-}" ]]; then
    warn "RELATIVE_UNIT_TESTS_DIR not set; falling back to 'tests/unit'"
  fi

  mapfile -t lambda_dirs < <(find . -maxdepth 1 -mindepth 1 -type d -not -name '.*' | sort)

  if [[ ${#lambda_dirs[@]} -eq 0 ]]; then
    info "No Lambda directories detected; nothing to test"
    return 0
  fi

  local lambda_dir
  local failures=0

  for lambda_dir in "${lambda_dirs[@]}"; do
    lambda_dir="${lambda_dir#./}"
    if ! run_tests_for_lambda "${lambda_dir}"; then
      failures=$((failures + 1))
    fi
  done

  if [[ ${failures} -ne 0 ]]; then
    fail "${failures} Lambda project(s) had failing unit tests"
  fi

  info "Unit tests completed successfully"
}

main "$@"
