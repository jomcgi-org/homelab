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

# Resolve one repo:tag to its manifest digest, retrying a few times. The fresh
# tags are pushed moments earlier in this same push_all run, so a first read can
# race ghcr propagation, and a burst of reads across all charts can hit
# transient rate limits (observed: monolith resolved but monolith-public did not
# in the same run under the same auth). Retry with backoff; a genuinely gone tag
# (an old published version whose image was GC'd) still ends UNRESOLVED, which
# the caller treats as "skip", not "fail".
_crane_digest() {
  local ref="$1" i d
  for i in 1 2 3; do
    if d=$("$CRANE" digest "$ref" 2>/dev/null) && [[ -n "$d" ]]; then
      printf '%s' "$d"
      return 0
    fi
    if [[ $i -lt 3 ]]; then sleep 3; fi
  done
  printf 'UNRESOLVED'
}

# Emit sorted "repo=digest" lines for each ghcr.io/jomcgi image pinned in a
# chart. Args are passed straight to `helm show values` (a .tgz path, or
# "REPO/NAME --version X"). The pinned tag embeds a build timestamp so it
# changes every build even for identical content; resolving it to the manifest
# digest gives a content-stable identity to compare. UNRESOLVED tags let the
# caller skip rather than false-fail on a transient registry error.
_image_digests() {
  "$HELM" show values "$@" 2>/dev/null | awk '
    /^[[:space:]]*repository:[[:space:]]*/ { repo=$2 }
    /^[[:space:]]*tag:[[:space:]]*/ && repo!="" { print repo"\t"$2; repo="" }' |
    while IFS="$(printf '\t')" read -r repo tag; do
      case "$repo" in
        ghcr.io/jomcgi/*)
          printf '%s=%s\n' "$repo" "$(_crane_digest "${repo}:${tag}")"
          ;;
      esac
    done | sort
}

# Fail the push with the exact bump command. Reads CHART_NAME / CHART_VERSION /
# ABS_CHART_DIR / CHART_DIR (all set before this is ever called). bump-chart.sh
# takes the project dir for the deploy/ convention, or the chart dir itself when
# application.yaml is colocated.
_fail_missed_bump() {
  local reason="$1" project_dir
  if [[ -f "${ABS_CHART_DIR}/application.yaml" ]]; then
    project_dir="$CHART_DIR"
  else
    project_dir=$(dirname "$CHART_DIR")
  fi
  {
    echo "ERROR: ${CHART_NAME} ${CHART_VERSION} is already published, but ${reason}."
    echo "This merge will NOT deploy until the chart version is bumped."
    echo ""
    echo "Fix: in a fresh worktree run"
    echo "  bazel/tools/git/bump-chart.sh ${project_dir}"
    echo "then commit and open a bump PR (auto-merge is fine)."
  } >&2
  exit 1
}

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
        # A merge that reuses an already-published chart version deploys NOTHING
        # of its own. Two independent detectors catch that here; either failing
        # is enough, so a missed bump surfaces in minutes instead of later as an
        # OutOfSync mystery or a rollout that silently never happened.

        # (1) Image-content detector: resolve the pinned image tags in the fresh
        # chart and the already-published chart to their manifest digests and
        # compare. This is INDEPENDENT of the bazel dependency query (2), which
        # can silently under-scope when `deps(...)` drops a package under
        # --keep_going (an image's external closure failing to preload) and then
        # report "no bump needed" for a real image change (this is exactly how a
        # frontend change once merged with no bump and never deployed). Digest
        # equality is content-stable, so this fails closed on any image change.
        if [[ -x "$CRANE" ]]; then
          FRESH_DIGESTS=$(_image_digests "$CHART_TGZ") || FRESH_DIGESTS=""
          PUB_DIGESTS=$(_image_digests "${REPOSITORY}/${CHART_NAME}" --version "${CHART_VERSION}") || PUB_DIGESTS=""
          if [[ "$FRESH_DIGESTS" == *UNRESOLVED* ]] || [[ "$PUB_DIGESTS" == *UNRESOLVED* ]]; then
            echo "WARNING: could not resolve all image digests for ${CHART_NAME}; relying on the version-scoped detector only." >&2
          elif [[ -n "$FRESH_DIGESTS" ]] && [[ -n "$PUB_DIGESTS" ]] && [[ "$FRESH_DIGESTS" != "$PUB_DIGESTS" ]]; then
            _fail_missed_bump "its pinned image digests differ from the published chart (an image was rebuilt)"
          fi
        fi

        # (2) Version-scoped detector: if conventional commits since this version
        # was last set touch the chart's dependency closure, a bump is due. This
        # also covers chart-template-only changes (which do not change any image
        # digest, so detector (1) would not catch them).
        if [[ "$CAN_VERSION" == "true" ]]; then
          NEEDED_VERSION=$(cd "$WORKSPACE" && "$CHART_VERSION_SH" "$CHART_DIR" "//${CHART_DIR}:chart.package") || NEEDED_VERSION=""
          if [[ -n "$NEEDED_VERSION" ]] && [[ "$NEEDED_VERSION" != "$CHART_VERSION" ]]; then
            _fail_missed_bump "commits since that version touch this chart's dependency closure (computed next version: ${NEEDED_VERSION})"
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
    # PR-time missed-bump guard. The version-scoped auto-bump above declined to
    # bump, but an image can be rebuilt to a new digest without touching the
    # bazel dependency closure that detector queries (a shared base-image change
    # under --keep_going), which then merges and only fails main's Push images
    # post-merge, wedging the NEXT person's deploy. Run the content-stable digest
    # check now so the PR cannot merge un-bumped. The guard fails OPEN on a
    # registry/resolution hiccup, but fails CLOSED when origin/main claims this
    # same version and it never shows up in the registry: that is the
    # rebase-merge collision signature (another PR claimed the version first
    # and this branch's bump hunks were, or would be, silently dropped).
    #
    # ADVISORY, not blocking (2026-08-09). Two reasons. First, it currently
    # produces a false block: projects/embervm/runtimes/bazel builds to a
    # different digest on a PR branch than on main for identical inputs, so the
    # guard fires on PRs that change nothing relevant (issue #4594),
    # and bumping the chart does not help because the next PR build differs
    # again. Second, ADR platform/009 decision 1 retires this guard outright:
    # once the chart version is written post-merge, "merge code, then a
    # follow-up publish deploys it" is the normal flow and there is no
    # pre-merge bump to miss. Blocking every PR on a known-false signal in the
    # window before that lands is the wrong trade. The comparison still RUNS
    # and still prints which digests differ, so the signal is not lost.
    if [[ -n "$CHECK_MISSED_BUMP" ]] && [[ -f "$CHECK_MISSED_BUMP" ]] && \
       [[ -n "$ABS_CHART_DIR" ]] && [[ -f "${ABS_CHART_DIR}/Chart.yaml" ]]; then
      GUARD_CHART_NAME=$(grep '^name:' "${ABS_CHART_DIR}/Chart.yaml" | head -1 | awk '{print $2}' | tr -d '"')
      if [[ -f "${ABS_CHART_DIR}/application.yaml" ]]; then
        GUARD_PROJECT_DIR="$CHART_DIR"
      else
        GUARD_PROJECT_DIR=$(dirname "$CHART_DIR")
      fi
      if [[ -n "$GUARD_CHART_NAME" ]]; then
        # origin/main's claimed version is what lets the guard distinguish
        # "this PR carries the bump" (skip) from "main claims this version but
        # has not published it" (wait, then fail closed). Best effort: with no
        # origin/main ref the guard keeps the legacy skip-when-unpublished
        # behavior.
        (cd "$WORKSPACE" && git fetch --quiet origin main 2>/dev/null) || true
        MAIN_CHART_VERSION=$(cd "$WORKSPACE" && git show "origin/main:${CHART_DIR}/Chart.yaml" 2>/dev/null | grep '^version:' | head -1 | awk '{print $2}' | tr -d '"') || MAIN_CHART_VERSION=""
        # Pass HELM/CRANE via `env`, not a command prefix: both are readonly in
        # this shell, and a `HELM=... cmd` prefix is a shell assignment that
        # bash rejects with "HELM: readonly variable". `env` sets them only in
        # the child's environment.
        env HELM="$HELM" CRANE="$CRANE" REPOSITORY="$REPOSITORY" \
          MAIN_CHART_VERSION="$MAIN_CHART_VERSION" \
          bash "$CHECK_MISSED_BUMP" "$GUARD_CHART_NAME" "$CURRENT_VERSION" "$CHART_TGZ" "$GUARD_PROJECT_DIR" \
          || echo "check-missed-bump: ADVISORY ONLY, not failing the build (see the comment above)." >&2
      fi
    fi
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
