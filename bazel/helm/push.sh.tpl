#!/usr/bin/env bash
# Push a packaged Helm chart to an OCI registry
# Template substitutions: {{HELM}}, {{CRANE}}, {{CHART_TGZ}}, {{REPOSITORY}}, {{CHART_VERSION_SH}}, {{CHART_DIR}}, {{CHECK_MISSED_BUMP}}

set -o errexit -o nounset -o pipefail

# Bazel runfiles setup
RUNFILES_DIR="${RUNFILES_DIR:-}"
if [[ -z "$RUNFILES_DIR" ]]; then
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

readonly HELM="$(rlocation "{{HELM}}")"
readonly CRANE="$(rlocation "{{CRANE}}")"
readonly CHART_TGZ="$(rlocation "{{CHART_TGZ}}")"
readonly CHECK_MISSED_BUMP="$(rlocation "{{CHECK_MISSED_BUMP}}")"
REPOSITORY="{{REPOSITORY}}"
CHART_VERSION_SH="{{CHART_VERSION_SH}}"
CHART_DIR="{{CHART_DIR}}"

# Resolve chart-version.sh via rlocation if it's a runfiles path
if [[ -n "$CHART_VERSION_SH" ]] && ! [[ -x "$CHART_VERSION_SH" ]]; then
  CHART_VERSION_SH="$(rlocation "$CHART_VERSION_SH" 2>/dev/null || echo "")"
fi

# Parse command line args
while (( $# > 0 )); do
  case $1 in
    (-r|--repository)
      REPOSITORY="$2"
      shift 2;;
    (--repository=*)
      REPOSITORY="${1#--repository=}"
      shift;;
    (*)
      echo "Unknown argument: $1" >&2
      exit 1;;
  esac
done

# Digest comparison used to live here, as a pair of helpers that resolved each
# chart's pinned image TAGS through `crane digest`. Both are gone: the only
# caller now runs check-missed-bump.sh, which reads the pinned digest out of the
# chart values and so needs no registry read at all. See the call site for why
# the registry round trip was unfixable on main (it raced the image pushes in
# the same multirun and fell open to a warning). CRANE is still resolved above
# because the PR-side call site passes it through.

# _fail_missed_bump lived here. It told the author to run bump-chart.sh, which
# is advice that no longer applies: with the version computed post-merge there
# is no bump for a PR to miss. CHECK_MISSED_BUMP above is now unused and is
# unwired, along with check-missed-bump.sh and bump-chart.sh themselves, in the
# follow-up cleanup for ADR platform/009 decision 1.

# --- Determine branch and workspace ---
PUSH_TGZ="$CHART_TGZ"

# BUILD_WORKSPACE_DIRECTORY is set by `bazel run` and points to the repo root.
# CHART_DIR is relative (e.g., "projects/agent_platform/chart"), so prefix it.
WORKSPACE="${BUILD_WORKSPACE_DIRECTORY:-}"
if [[ -n "$WORKSPACE" ]] && [[ -n "$CHART_DIR" ]]; then
  ABS_CHART_DIR="${WORKSPACE}/${CHART_DIR}"
  CURRENT_BRANCH=$(cd "$WORKSPACE" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
else
  ABS_CHART_DIR=""
  CURRENT_BRANCH="unknown"
fi

CAN_VERSION="false"
if [[ -n "$CHART_VERSION_SH" ]] && [[ -n "$ABS_CHART_DIR" ]] && [[ -x "$CHART_VERSION_SH" ]] && [[ -d "$ABS_CHART_DIR" ]]; then
  CAN_VERSION="true"
fi

if [[ "$CURRENT_BRANCH" == "main" ]]; then
  # --- Main branch: compute the version, publish it, record it for write-back ---
  #
  # ADR platform/009 decision 1: the chart version is an OUTPUT of merging, not
  # an input carried on the PR branch. PRs no longer touch Chart.yaml `version:`
  # or deploy/application.yaml `targetRevision:`, so there is no bump to miss
  # pre-merge and no shared line for two PRs to conflict on.
  #
  # This step deliberately does NOT touch git. push_all is a multirun with
  # `jobs = 0`, so ~24 of these run CONCURRENTLY in one workspace: a commit here
  # would race every other chart's on a shared index, and 24 jobs would race
  # main's ref. Each chart records its published version to its own file
  # instead, and one step after the multirun (write-back-versions.sh) makes a
  # single commit for all of them. See bazel/images/BUILD and buildbuddy.yaml.
  #
  # Publishing stays IDEMPOTENT: if the computed version is already in the
  # registry there is nothing to do. Re-publishing an existing version with
  # freshly stamped image tags mutates a deployed chart in place, which wedges
  # ArgoCD into a permanent sync=OutOfSync + operationState=Succeeded state: the
  # repo-server serves its cached copy for sync while diffing against the
  # mutated re-pull (ADR platform/011).
  echo "On main: computing the post-merge chart version"
  if [[ -n "$ABS_CHART_DIR" ]] && [[ -f "${ABS_CHART_DIR}/Chart.yaml" ]]; then
    CHART_NAME=$(grep '^name:' "${ABS_CHART_DIR}/Chart.yaml" | head -1 | awk '{print $2}' | tr -d '"')
    CURRENT_VERSION=$(grep '^version:' "${ABS_CHART_DIR}/Chart.yaml" | head -1 | awk '{print $2}' | tr -d '"')

    # Loop guard. The write-back commit touches ONLY Chart.yaml and
    # application.yaml, so the next run rebuilds identical image digests and
    # chart-version.sh (which already skips chart-version-bot subjects) finds no
    # qualifying commits, returns the same version, and the registry check below
    # short-circuits. This author check is belt and braces on top of that, and
    # mirrors what the format stage already does for ci-format-bot.
    LAST_AUTHOR=$(cd "$WORKSPACE" && git log -1 --format='%an' 2>/dev/null || echo "")
    if [[ "$LAST_AUTHOR" == "chart-version-bot" ]]; then
      echo "HEAD is the chart-version-bot write-back; nothing to publish."
      exit 0
    fi

    if [[ -n "$CHART_NAME" ]] && [[ -n "$CURRENT_VERSION" ]]; then
      # The computed version is a function of THIS COMMIT, so two concurrent
      # publishes cannot land on the same number. See chart-version.sh.
      CHART_VERSION="$CURRENT_VERSION"
      if [[ "$CAN_VERSION" == "true" ]]; then
        COMPUTED_VERSION=$(cd "$WORKSPACE" && "$CHART_VERSION_SH" "$CHART_DIR" "//${CHART_DIR}:chart.package") || COMPUTED_VERSION=""
        if [[ -n "$COMPUTED_VERSION" ]]; then
          CHART_VERSION="$COMPUTED_VERSION"
        else
          echo "WARNING: could not compute a version for ${CHART_NAME}; keeping Chart.yaml's ${CURRENT_VERSION}." >&2
        fi
      fi

      set +e
      SHOW_OUT=$("${HELM}" show chart "${REPOSITORY}/${CHART_NAME}" --version "${CHART_VERSION}" 2>&1)
      SHOW_RC=$?
      set -e
      if [[ $SHOW_RC -eq 0 ]]; then
        echo "Chart ${CHART_NAME} ${CHART_VERSION} is already published; nothing to publish."
        exit 0
      fi
      if echo "$SHOW_OUT" | grep -qiE 'not found|manifest unknown|404'; then
        echo "Publishing ${CHART_NAME}: ${CURRENT_VERSION} -> ${CHART_VERSION}"
      else
        echo "WARNING: could not check whether ${CHART_NAME} ${CHART_VERSION} already exists; pushing anyway (legacy behavior). Check output:"
        echo "$SHOW_OUT" | head -5
      fi

      # Repackage at the computed version: the bazel-built .tgz still carries
      # Chart.yaml's version, which is now the PREVIOUS release. Only the
      # version line changes, so the pinned image digests are untouched, which
      # is what makes the next publish a no-op.
      if [[ "$CHART_VERSION" != "$CURRENT_VERSION" ]]; then
        MAIN_WORK_DIR=$(mktemp -d)
        trap 'rm -rf "$MAIN_WORK_DIR"' EXIT
        tar -xzf "$CHART_TGZ" -C "$MAIN_WORK_DIR"
        PKG_NAME=$(ls "$MAIN_WORK_DIR")
        sed "s/^version:.*/version: ${CHART_VERSION}/" "$MAIN_WORK_DIR/${PKG_NAME}/Chart.yaml" > "$MAIN_WORK_DIR/${PKG_NAME}/Chart.yaml.tmp"
        mv "$MAIN_WORK_DIR/${PKG_NAME}/Chart.yaml.tmp" "$MAIN_WORK_DIR/${PKG_NAME}/Chart.yaml"
        "$HELM" package "$MAIN_WORK_DIR/${PKG_NAME}" --destination "$MAIN_WORK_DIR" >/dev/null
        PUSH_TGZ="$MAIN_WORK_DIR/${PKG_NAME}-${CHART_VERSION}.tgz"
      fi

      # Record for the batched write-back: one file per chart, so concurrent
      # multirun jobs never write the same path.
      #
      # The location is DERIVED FROM THE WORKSPACE, not passed in an env var.
      # An env var would have to survive `bazel run` into the target's
      # environment, and if it ever did not, this would record nothing, the
      # write-back would report "nothing to write back", and NO CHART WOULD EVER
      # DEPLOY AGAIN with every step still green. BUILD_WORKSPACE_DIRECTORY is
      # set by `bazel run` itself and is already load-bearing above, so there is
      # no second thing to keep working.
      RECORD_DIR="${WORKSPACE}/.chart-version-records"
      mkdir -p "$RECORD_DIR"
      printf '%s %s\n' "$CHART_DIR" "$CHART_VERSION" > "${RECORD_DIR}/${CHART_NAME}"
    fi
  fi
elif [[ "$CAN_VERSION" == "true" ]]; then
  # --- PR branch: publish an ephemeral chart only ---
  # PRs no longer compute or commit a chart version (ADR platform/009 decision
  # 1). With no version in the diff there is no bump to miss, so the pre-merge
  # missed-bump guard is retired here too: it was already advisory-only, and
  # the version-collision case it existed to catch is now impossible. Per-PR
  # verification loses nothing, because the ephemeral 0.0.0-dev chart below is
  # still built and pushed exactly as before.

  # Re-package chart with semver-compatible pre-release tag for OCI push (PRs use ephemeral tags).
  # Prefix the short SHA with `g` (git-describe convention) so it is always an
  # alphanumeric SemVer pre-release identifier. An all-numeric short SHA with a
  # leading zero (e.g. 00303542) would otherwise be parsed as a numeric identifier,
  # which SemVer forbids from having leading zeroes, and Helm rejects the version.
  DATESTAMP="0.0.0-dev.$(date -u '+%Y%m%d%H%M%S').g$(cd "$WORKSPACE" && git rev-parse --short HEAD)"
  WORK_DIR=$(mktemp -d)
  tar -xzf "$CHART_TGZ" -C "$WORK_DIR"
  CHART_NAME=$(ls "$WORK_DIR")
  sed "s/^version:.*/version: ${DATESTAMP}/" "$WORK_DIR/$CHART_NAME/Chart.yaml" > "$WORK_DIR/$CHART_NAME/Chart.yaml.tmp"
  mv "$WORK_DIR/$CHART_NAME/Chart.yaml.tmp" "$WORK_DIR/$CHART_NAME/Chart.yaml"
  PUSH_TGZ="$WORK_DIR/${CHART_NAME}-${DATESTAMP}.tgz"
  "$HELM" package "$WORK_DIR/$CHART_NAME" --destination "$WORK_DIR"
  trap "rm -rf '$WORK_DIR'" EXIT
fi

echo "Pushing Helm chart: ${PUSH_TGZ}"
echo "  Repository: ${REPOSITORY}"

"${HELM}" push "${PUSH_TGZ}" "${REPOSITORY}"

echo "Successfully pushed chart to ${REPOSITORY}"
