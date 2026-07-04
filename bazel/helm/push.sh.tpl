#!/usr/bin/env bash
# Push a packaged Helm chart to an OCI registry
# Template substitutions: {{HELM}}, {{CHART_TGZ}}, {{REPOSITORY}}, {{CHART_VERSION_SH}}, {{CHART_DIR}}

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
readonly CHART_TGZ="$(rlocation "{{CHART_TGZ}}")"
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
  # --- Main branch: publish the semver from Chart.yaml (bumped in the PR) ---
  # Publishing is IDEMPOTENT: if this version already exists in the registry,
  # skip the push. Re-publishing an existing version with freshly stamped image
  # tags mutates a deployed chart in place, which wedges ArgoCD into a permanent
  # sync=OutOfSync + operationState=Succeeded state: the repo-server serves its
  # cached copy for sync while diffing against the mutated re-pull.
  echo "On main: publishing chart at the semver from Chart.yaml"
  if [[ -n "$ABS_CHART_DIR" ]] && [[ -f "${ABS_CHART_DIR}/Chart.yaml" ]]; then
    CHART_NAME=$(grep '^name:' "${ABS_CHART_DIR}/Chart.yaml" | head -1 | awk '{print $2}' | tr -d '"')
    CHART_VERSION=$(grep '^version:' "${ABS_CHART_DIR}/Chart.yaml" | head -1 | awk '{print $2}' | tr -d '"')
    if [[ -n "$CHART_NAME" ]] && [[ -n "$CHART_VERSION" ]]; then
      set +e
      SHOW_OUT=$("${HELM}" show chart "${REPOSITORY}/${CHART_NAME}" --version "${CHART_VERSION}" 2>&1)
      SHOW_RC=$?
      set -e
      if [[ $SHOW_RC -eq 0 ]]; then
        echo "Chart ${CHART_NAME} ${CHART_VERSION} is already published; skipping push."
        # Missed-bump detector: if commits since this version was last set touch
        # the chart's dependency closure, this merge deploys NOTHING until a bump
        # lands. Fail loudly now instead of surfacing later as an OutOfSync
        # mystery or a rollout that silently never happened.
        if [[ "$CAN_VERSION" == "true" ]]; then
          NEEDED_VERSION=$(cd "$WORKSPACE" && "$CHART_VERSION_SH" "$CHART_DIR" "//${CHART_DIR}:chart.package") || NEEDED_VERSION=""
          if [[ -n "$NEEDED_VERSION" ]] && [[ "$NEEDED_VERSION" != "$CHART_VERSION" ]]; then
            # bump-chart.sh takes the project dir for the deploy/ convention,
            # or the chart dir itself when application.yaml is colocated.
            if [[ -f "${ABS_CHART_DIR}/application.yaml" ]]; then
              PROJECT_DIR="$CHART_DIR"
            else
              PROJECT_DIR=$(dirname "$CHART_DIR")
            fi
            {
              echo "ERROR: ${CHART_NAME} ${CHART_VERSION} is already published, but commits since"
              echo "that version touch this chart's dependency closure (computed next"
              echo "version: ${NEEDED_VERSION}). This merge will NOT deploy until the chart"
              echo "version is bumped."
              echo ""
              echo "Fix: in a fresh worktree run"
              echo "  bazel/tools/git/bump-chart.sh ${PROJECT_DIR}"
              echo "then commit and open a bump PR (auto-merge is fine)."
            } >&2
            exit 1
          fi
        fi
        echo "No releasable changes since ${CHART_VERSION}; nothing to publish."
        exit 0
      fi
      if echo "$SHOW_OUT" | grep -qiE 'not found|manifest unknown|404'; then
        echo "Version ${CHART_VERSION} not in registry yet; publishing."
      else
        echo "WARNING: could not check whether ${CHART_NAME} ${CHART_VERSION} already exists; pushing anyway (legacy behavior). Check output:"
        echo "$SHOW_OUT" | head -5
      fi
    fi
  fi
elif [[ "$CAN_VERSION" == "true" ]]; then
  # --- PR branch: compute version bump, commit to PR, push with datestamp ---
  BAZEL_PKG="//${CHART_DIR}:chart.package"
  CURRENT_VERSION=$(grep '^version:' "${ABS_CHART_DIR}/Chart.yaml" | head -1 | awk '{print $2}' | tr -d '"')

  # Compute next semver version from conventional commits
  NEW_VERSION=$(cd "$WORKSPACE" && "$CHART_VERSION_SH" "$CHART_DIR" "$BAZEL_PKG") || true

  # Commit version bump + targetRevision update to the PR branch if changed
  if [[ -n "$NEW_VERSION" ]] && [[ "$NEW_VERSION" != "$CURRENT_VERSION" ]]; then
    echo "Chart version bump: ${CURRENT_VERSION} -> ${NEW_VERSION}"
    ABS_CHART_YAML="${ABS_CHART_DIR}/Chart.yaml"

    sed "s/^version:.*/version: ${NEW_VERSION}/" "$ABS_CHART_YAML" > "${ABS_CHART_YAML}.tmp"
    mv "${ABS_CHART_YAML}.tmp" "$ABS_CHART_YAML"

    CHART_NAME_LOWER=$(grep '^name:' "$ABS_CHART_YAML" | head -1 | awk '{print $2}' | tr -d '"')
    cd "$WORKSPACE"
    git config user.name "chart-version-bot"
    git config user.email "chart-version-bot@users.noreply.github.com"
    git add "${CHART_DIR}/Chart.yaml"

    # Also update targetRevision in the ArgoCD Application so it deploys the new chart version.
    # Convention 1: chart at projects/<svc>/chart → deploy at projects/<svc>/deploy/application.yaml
    # Convention 2: chart and application.yaml colocated in the same directory
    DEPLOY_APP_YAML="$(dirname "$ABS_CHART_DIR")/deploy/application.yaml"
    if [[ ! -f "$DEPLOY_APP_YAML" ]] && [[ -f "${ABS_CHART_DIR}/application.yaml" ]]; then
      DEPLOY_APP_YAML="${ABS_CHART_DIR}/application.yaml"
    fi
    if [[ -f "$DEPLOY_APP_YAML" ]]; then
      CURRENT_TARGET=$(grep 'targetRevision:' "$DEPLOY_APP_YAML" | head -1 | awk '{print $2}' | tr -d '"')
      if [[ -n "$CURRENT_TARGET" ]] && [[ "$CURRENT_TARGET" != "$NEW_VERSION" ]]; then
        echo "Updating targetRevision: ${CURRENT_TARGET} -> ${NEW_VERSION}"
        sed "s/targetRevision: ${CURRENT_TARGET}/targetRevision: ${NEW_VERSION}/" "$DEPLOY_APP_YAML" > "${DEPLOY_APP_YAML}.tmp"
        mv "${DEPLOY_APP_YAML}.tmp" "$DEPLOY_APP_YAML"
        # git add using the path relative to workspace root
        RELATIVE_APP_YAML="${DEPLOY_APP_YAML#${WORKSPACE}/}"
        git add "$RELATIVE_APP_YAML"
      fi
    fi

    git commit -m "chore(${CHART_NAME_LOWER}): bump chart version to ${NEW_VERSION}"
    git push origin HEAD:"${CURRENT_BRANCH}"
    echo "Version bump committed and pushed to ${CURRENT_BRANCH}"
  else
    echo "Chart version unchanged at ${CURRENT_VERSION}"
  fi

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
