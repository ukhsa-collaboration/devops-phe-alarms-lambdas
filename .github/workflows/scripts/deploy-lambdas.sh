#!/usr/bin/env bash

set -euo pipefail

# We're calling this script from the lambdas directory
# shellcheck disable=SC1091
source ../.github/workflows/scripts/functions.sh

export AWS_PAGER=""

require_env() {
  local var
  for var in "$@"; do
    if [[ -z "${!var:-}" ]]; then
      fail "Environment variable '${var}' must be set"
    fi
  done
}

determine_function_name() {
  local dir=$1
  local custom_name_file

  for custom_name_file in ".lambda-name" "lambda-name" "lambda_function_name"; do
    if [[ -f "${dir}/${custom_name_file}" ]]; then
      local name
      name="$(<"${dir}/${custom_name_file}")"
      if [[ -n "${name// }" ]]; then
        echo "${name//[$'\n\r']}"
        return
      fi
    fi
  done

  echo "${LAMBDA_PREFIX}""${ENVIRONMENT}"-lambda-"$(basename "${dir}")"
}

# TODO: Implement
run_function_integration_tests() {
  echo "Would have run integration tests"
}

# TODO: Implement
run_function_unit_tests() {
  echo "Would have run unit tests"
}

build_and_deploy_lambda() {
  local lambda_dir=$1
  local version=$2
  local bucket=$3
  local aws_account_id=$4
  local aws_region=$5
  local python_version=$6
  local python_platform=$7
  local s3_key_prefix=$8

  (
    set -euo pipefail

    if [[ ! -d "${lambda_dir}" ]]; then
      warn "Skipping '${lambda_dir}' because it is not a directory"
      exit 0
    fi

    if [[ ! -d "${lambda_dir}/app" ]]; then
      warn "Skipping '${lambda_dir}' because it does not contain an 'app/' directory"
      exit 0
    fi

    local function_name
    function_name="$(determine_function_name "${lambda_dir}")"
    local s3_key="${s3_key_prefix}/${function_name}/${version}.zip"
    local workspace
    workspace="$(mktemp -d "${TMPDIR:-/tmp}/lambda-build-${function_name}-XXXXXX")"

    info "Packaging Lambda '${function_name}' from '${lambda_dir}'"

    pushd "${lambda_dir}" >/dev/null || fail "Unable to enter directory '${lambda_dir}'"
    trap '
      popd >/dev/null || true
      rm -rf "${workspace}"
    ' EXIT

    if [[ -f "tests/unit" ]]; then
      run_function_unit_tests
    fi

    local requirements_source="requirements.txt"
    if [[ ! -f "${requirements_source}" ]]; then
      if [[ -f "pyproject.toml" ]]; then
        info "Generating requirements.txt using uv for '${function_name}'"
        uv export --frozen --no-dev --no-editable -o "${requirements_source}"
      else
        fail "No requirements.txt or pyproject.toml found in '${lambda_dir}'"
      fi
    fi

    local cache_args=()
    if [[ -n "${PIP_CACHE_DIR:-}" ]]; then
      cache_args+=(--cache-dir "${PIP_CACHE_DIR}")
    fi

    info "Installing dependencies for '${function_name}'"
    uv pip install \
      --no-installer-metadata \
      --no-compile-bytecode \
      --python-platform "${python_platform}" \
      --python "${python_version}" \
      --target "${workspace}" \
      -r "${requirements_source}" \
      "${cache_args[@]}"

    info "Syncing application code for '${function_name}'"
    rsync -a \
      --exclude '__pycache__/' \
      --exclude '.pytest_cache/' \
      --exclude '.mypy_cache/' \
      --exclude '.venv/' \
      --exclude 'build/' \
      --exclude '*.egg-info/' \
      --exclude '*.zip' \
      --exclude 'tests/' \
      --exclude 'requirements.txt' \
      --exclude 'uv.lock' \
      --exclude 'pyproject.toml' \
      ./ \
      "${workspace}/"

    local package_path="${PWD}/${version}.zip"
    rm -f "${package_path}"

    info "Creating deployment package for '${function_name}'"
    (cd "${workspace}" && zip -rq "${package_path}" .)

    info "Uploading package to s3://${bucket}/${s3_key}"
    aws s3 cp "${package_path}" "s3://${bucket}/${s3_key}"

    local lambda_arn="arn:aws:lambda:${aws_region}:${aws_account_id}:function:${function_name}"
    info "Updating Lambda function code for '${lambda_arn}'"
    aws lambda update-function-code \
      --function-name "${lambda_arn}" \
      --s3-bucket "${bucket}" \
      --s3-key "${s3_key}" \
      >/dev/null

    info "Lambda '${function_name}' updated to version '${version}'"

    if [[ -f "tests/integration" ]]; then
      run_function_integration_tests
    fi

    rm -f "${package_path}"
  )
}

main() {
  require_cmd uv aws zip rsync
  require_env VERSION BUCKET_NAME AWS_ACCOUNT_ID AWS_REGION LAMBDA_PREFIX ENVIRONMENT

  local python_version="${PYTHON_VERSION:-3.12}"
  local python_platform="${PYTHON_PLATFORM:-x86_64-manylinux2014}"
  local s3_key_prefix="${S3_KEY_PREFIX:-builds}"

  local lambda_dirs=()
  if [[ -n "${LAMBDA_DIRS:-}" ]]; then
    # shellcheck disable=SC2206
    lambda_dirs=(${LAMBDA_DIRS})
  else
    # note: mapfile / readarray isn't included with ZSH and the alternative approach is far less readable
    # so install bash 5.0 with `brew install bash` if you want to run it on macOS
    mapfile -t lambda_dirs < <(find . -maxdepth 1 -mindepth 1 -type d -not -name '.*' | sort)
  fi

  if [[ ${#lambda_dirs[@]} -eq 0 ]]; then
    info "No Lambda directories detected in $(pwd); nothing to deploy"
    return 0
  fi

  info "Found ${#lambda_dirs[@]} Lambda directory(ies) to process"

  local dir
  for dir in "${lambda_dirs[@]}"; do
    dir="${dir#./}"
    # shellcheck disable=SC2153
    build_and_deploy_lambda "${dir}" "${VERSION}" "${BUCKET_NAME}" "${AWS_ACCOUNT_ID}" "${AWS_REGION}" "${python_version}" "${python_platform}" "${s3_key_prefix}"
  done
}

main "$@"
