#!/usr/bin/env bash
# Push script for multi-platform apko images
# Based on rules_oci push.sh.tpl but adds --index for multi-platform images

set -o errexit -o nounset -o pipefail

# Bazel runfiles setup - adapted from rules_oci for bazel run compatibility
RUNFILES_DIR="${RUNFILES_DIR:-}"
if [[ -z "$RUNFILES_DIR" ]]; then
  # When run via bazel run, set RUNFILES_DIR to script location + .runfiles
  RUNFILES_DIR="$0.runfiles"
fi

if [[ -f "${RUNFILES_DIR}/bazel_tools/tools/bash/runfiles/runfiles.bash" ]]; then
  source "${RUNFILES_DIR}/bazel_tools/tools/bash/runfiles/runfiles.bash"
elif [[ -f "${RUNFILES_MANIFEST_FILE:-/dev/null}" ]]; then
  source "$(grep -m1 "^bazel_tools/tools/bash/runfiles/runfiles.bash " \
    "$RUNFILES_MANIFEST_FILE" | cut -d ' ' -f 2-)"
else
  echo >&2 "ERROR: cannot find @bazel_tools//tools/bash/runfiles:runfiles.bash"
  exit 1
fi

readonly CRANE="$(rlocation "{{CRANE}}")"
readonly JQ="$(rlocation "{{JQ}}")"
readonly IMAGE_DIR="$(rlocation "{{IMAGE_DIR}}")"
readonly REPOSITORY_FILE="$(rlocation "{{REPOSITORY_FILE}}")"
readonly TAGS_FILE="$(rlocation "{{TAGS_FILE}}")"

# rlocation hands back the path it was given when it cannot resolve, so an
# unbuilt runfiles tree arrives at jq below as a workspace-relative path and
# reads as a missing FILE rather than as a failed lookup:
#
#   jq: error: Could not open file projects/monolith/frontend/image/index.json
#
# That sentence cost #4685 a wrong fix, because it looks like the layout was
# never downloaded when in fact it was never named. IMAGE_DIR is a directory,
# which is precisely the case a runfiles MANIFEST cannot answer, so this fires
# exactly when the caller forgot --build_runfile_links. preset.bazelrc sets
# --nobuild_runfile_links repo-wide, which makes forgetting it the default.
if [[ ! -d "${IMAGE_DIR}" ]]; then
  echo >&2 "ERROR: the image layout did not resolve through runfiles."
  echo >&2 "       IMAGE_DIR=${IMAGE_DIR}"
  echo >&2 "       Run this target with --build_runfile_links. rlocation cannot"
  echo >&2 "       resolve a DIRECTORY from RUNFILES_MANIFEST_FILE alone, and"
  echo >&2 "       preset.bazelrc disables the runfiles tree repo-wide."
  exit 1
fi

# Read repository
REPOSITORY=$(cat "${REPOSITORY_FILE}")

# Parse command line args
GLOBAL_FLAGS=()
TAGS_OVERRIDE=()

while (( $# > 0 )); do
  case $1 in
    (-r|--repository)
      REPOSITORY="$2"
      shift 2;;
    (--repository=*)
      REPOSITORY="${1#--repository=}"
      shift;;
    (-t|--tag)
      TAGS_OVERRIDE+=( "$2" )
      shift 2;;
    (--tag=*)
      TAGS_OVERRIDE+=( "${1#--tag=}" )
      shift;;
    (--allow-nondistributable-artifacts|--insecure|-v|--verbose)
      GLOBAL_FLAGS+=( "$1" )
      shift;;
    (--platform)
      GLOBAL_FLAGS+=( "--platform" "$2" )
      shift 2;;
    (*)
      echo "Unknown argument: $1" >&2
      exit 1;;
  esac
done

# Read tags from file and/or command line
TAGS=()
if [[ -f "${TAGS_FILE}" ]]; then
  while IFS= read -r tag; do
    [[ -n "${tag}" ]] && TAGS+=( "${tag}" )
  done < "${TAGS_FILE}"
fi
TAGS+=( "${TAGS_OVERRIDE[@]+"${TAGS_OVERRIDE[@]}"}" )

# Check if this is a multi-platform image
MANIFEST_COUNT=$("${JQ}" -r '.manifests | length' "${IMAGE_DIR}/index.json")

if [[ "${MANIFEST_COUNT}" -gt 1 ]]; then
  echo "Detected multi-platform image with ${MANIFEST_COUNT} manifests"
  # For multi-platform, push the index directly without specifying digest
  # crane will compute the index digest automatically
  "${CRANE}" push "${GLOBAL_FLAGS[@]+"${GLOBAL_FLAGS[@]}"}" --index "${IMAGE_DIR}" "${REPOSITORY}" --image-refs "${REFS_TMP:=$(mktemp)}"
  REFS="${REFS_TMP}"
else
  echo "Single platform image"
  # For single platform, get the manifest digest and push
  DIGEST=$("${JQ}" -r '.manifests[0].digest' "${IMAGE_DIR}/index.json")
  "${CRANE}" push "${GLOBAL_FLAGS[@]+"${GLOBAL_FLAGS[@]}"}" "${IMAGE_DIR}" "${REPOSITORY}@${DIGEST}" --image-refs "${REFS_TMP:=$(mktemp)}"
  REFS="${REFS_TMP}"
fi

# Tag the image
for tag in "${TAGS[@]+"${TAGS[@]}"}"
do
  echo "Tagging with: ${tag}"
  "${CRANE}" tag "${GLOBAL_FLAGS[@]+"${GLOBAL_FLAGS[@]}"}" $(cat "${REFS}") "${tag}"
done

rm -f "${REFS}"
echo "Successfully pushed ${REPOSITORY} with ${#TAGS[@]} tag(s)"
