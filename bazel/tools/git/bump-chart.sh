#!/usr/bin/env bash
# bump-chart.sh: race-free chart version bump for a service.
#
# Bumps <project>/chart/Chart.yaml `version:` AND the matching semver
# `targetRevision:` in <project>/deploy/application.yaml together, computing
# the new number from the version at origin/main tip (not the local checkout),
# so concurrent sessions cannot pick the same number twice. Optionally verifies
# the candidate version is not already published to the OCI registry.
#
# Usage:
#   bazel/tools/git/bump-chart.sh <projects/<svc>[/chart]> [options]
#
# Options:
#   --minor            bump the minor version (default: patch)
#   --major            bump the major version
#   --version X.Y.Z    use an explicit version (must be greater than the base)
#   --force            bump even if the branch already carries an unpublished bump
#
# Environment:
#   BUMP_CHART_SKIP_REGISTRY_CHECK=1  skip the OCI registry existence probe
#   BUMP_CHART_MAIN_VERSION=X.Y.Z    override the origin/main version (tests)
#   BUMP_CHART_REPO_ROOT=/path       override the repo root (tests)
#   BUMP_CHART_REPOSITORY=oci://...  override the chart registry
set -euo pipefail

REPOSITORY="${BUMP_CHART_REPOSITORY:-oci://ghcr.io/jomcgi/homelab/charts}"

usage() {
	grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,20p' >&2
	exit 1
}

PROJECT_ARG="${1:-}"
[[ -n "$PROJECT_ARG" ]] || usage
shift

BUMP_KIND="patch"
EXPLICIT_VERSION=""
FORCE="false"
while (($# > 0)); do
	case "$1" in
	--minor) BUMP_KIND="minor" ;;
	--major) BUMP_KIND="major" ;;
	--version)
		EXPLICIT_VERSION="${2:?--version needs an argument}"
		shift
		;;
	--force) FORCE="true" ;;
	*)
		echo "Unknown argument: $1" >&2
		usage
		;;
	esac
	shift
done

REPO_ROOT="${BUMP_CHART_REPO_ROOT:-$(git rev-parse --show-toplevel)}"
cd "$REPO_ROOT"

# Resolve the chart directory from the project argument.
PROJECT_ARG="${PROJECT_ARG%/}"
if [[ -f "${PROJECT_ARG}/Chart.yaml" ]]; then
	CHART_DIR="$PROJECT_ARG"
elif [[ -f "${PROJECT_ARG}/chart/Chart.yaml" ]]; then
	CHART_DIR="${PROJECT_ARG}/chart"
else
	echo "ERROR: no Chart.yaml under '${PROJECT_ARG}' or '${PROJECT_ARG}/chart'" >&2
	exit 1
fi
CHART_YAML="${CHART_DIR}/Chart.yaml"

# application.yaml conventions, mirroring bazel/helm/push.sh.tpl:
# projects/<svc>/chart -> projects/<svc>/deploy/application.yaml, or colocated.
APP_YAML="$(dirname "$CHART_DIR")/deploy/application.yaml"
if [[ ! -f "$APP_YAML" ]] && [[ -f "${CHART_DIR}/application.yaml" ]]; then
	APP_YAML="${CHART_DIR}/application.yaml"
fi
if [[ ! -f "$APP_YAML" ]]; then
	echo "ERROR: no deploy/application.yaml for ${CHART_DIR}" >&2
	exit 1
fi

read_version() {
	grep '^version:' "$1" | head -1 | awk '{print $2}' | tr -d '"'
}

CHART_NAME=$(grep '^name:' "$CHART_YAML" | head -1 | awk '{print $2}' | tr -d '"')
LOCAL_VERSION=$(read_version "$CHART_YAML")

# The base version comes from origin/main tip, not the local checkout: version
# numbers race between concurrent sessions, and picking from a stale base is
# how duplicate bump commits (silently dropped by rebase-merge) happen.
if [[ -n "${BUMP_CHART_MAIN_VERSION:-}" ]]; then
	MAIN_VERSION="$BUMP_CHART_MAIN_VERSION"
else
	git fetch origin main --quiet
	MAIN_VERSION=$(git show "origin/main:${CHART_YAML}" | grep '^version:' | head -1 | awk '{print $2}' | tr -d '"')
fi

if [[ ! "$MAIN_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || [[ ! "$LOCAL_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	echo "ERROR: could not parse versions (main: '${MAIN_VERSION}', local: '${LOCAL_VERSION}')" >&2
	exit 1
fi

# Numeric semver comparison: prints the greater of the two.
max_version() {
	printf '%s\n%s\n' "$1" "$2" | awk -F. '{ printf "%09d%09d%09d %s\n", $1, $2, $3, $0 }' | sort | tail -1 | awk '{print $2}'
}

BASE_VERSION=$(max_version "$LOCAL_VERSION" "$MAIN_VERSION")

if [[ "$BASE_VERSION" == "$LOCAL_VERSION" ]] && [[ "$LOCAL_VERSION" != "$MAIN_VERSION" ]] && [[ "$FORCE" != "true" ]] && [[ -z "$EXPLICIT_VERSION" ]]; then
	echo "Branch already carries an unpublished bump for ${CHART_NAME}: local ${LOCAL_VERSION} > main ${MAIN_VERSION}."
	echo "Nothing to do (use --force to bump past the local version)."
	exit 0
fi

increment() {
	local v="$1" kind="$2" major minor patch
	IFS=. read -r major minor patch <<<"$v"
	case "$kind" in
	major) printf '%d.0.0' "$((major + 1))" ;;
	minor) printf '%d.%d.0' "$major" "$((minor + 1))" ;;
	patch) printf '%d.%d.%d' "$major" "$minor" "$((patch + 1))" ;;
	esac
}

if [[ -n "$EXPLICIT_VERSION" ]]; then
	if [[ ! "$EXPLICIT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
		echo "ERROR: --version must be X.Y.Z (got '${EXPLICIT_VERSION}')" >&2
		exit 1
	fi
	if [[ "$(max_version "$EXPLICIT_VERSION" "$BASE_VERSION")" != "$EXPLICIT_VERSION" ]] || [[ "$EXPLICIT_VERSION" == "$BASE_VERSION" ]]; then
		echo "ERROR: --version ${EXPLICIT_VERSION} is not greater than the base ${BASE_VERSION}" >&2
		exit 1
	fi
	NEW_VERSION="$EXPLICIT_VERSION"
else
	NEW_VERSION=$(increment "$BASE_VERSION" "$BUMP_KIND")
fi

# Best-effort registry probe: if the candidate is already published (another
# session got there first), keep incrementing the patch until a free number is
# found. A probe failure is treated as "free" (network flake must not block).
if [[ "${BUMP_CHART_SKIP_REGISTRY_CHECK:-}" != "1" ]] && command -v helm >/dev/null 2>&1; then
	for _ in $(seq 1 20); do
		if helm show chart "${REPOSITORY}/${CHART_NAME}" --version "$NEW_VERSION" >/dev/null 2>&1; then
			echo "Version ${NEW_VERSION} already published in the registry; trying the next patch."
			NEW_VERSION=$(increment "$NEW_VERSION" "patch")
		else
			break
		fi
	done
fi

# The application.yaml must contain exactly one semver targetRevision (the OCI
# chart pin); git refs like `targetRevision: HEAD` or `main` are untouched.
SEMVER_TR_COUNT=$(grep -cE '^[[:space:]]*targetRevision:[[:space:]]*"?[0-9]+\.[0-9]+\.[0-9]+"?[[:space:]]*$' "$APP_YAML" || true)
if [[ "$SEMVER_TR_COUNT" -ne 1 ]]; then
	echo "ERROR: expected exactly 1 semver targetRevision in ${APP_YAML}, found ${SEMVER_TR_COUNT}; edit manually." >&2
	exit 1
fi
OLD_TR=$(grep -E '^[[:space:]]*targetRevision:[[:space:]]*"?[0-9]+\.[0-9]+\.[0-9]+"?[[:space:]]*$' "$APP_YAML" | head -1 | awk '{print $2}' | tr -d '"')
if [[ "$OLD_TR" != "$LOCAL_VERSION" ]]; then
	echo "WARNING: ${APP_YAML} targetRevision (${OLD_TR}) != Chart.yaml version (${LOCAL_VERSION}); setting both to ${NEW_VERSION}."
fi

sed "s/^version:.*/version: ${NEW_VERSION}/" "$CHART_YAML" >"${CHART_YAML}.tmp"
mv "${CHART_YAML}.tmp" "$CHART_YAML"
sed "s/targetRevision: ${OLD_TR}\$/targetRevision: ${NEW_VERSION}/" "$APP_YAML" >"${APP_YAML}.tmp"
mv "${APP_YAML}.tmp" "$APP_YAML"

echo "Bumped ${CHART_NAME}: ${LOCAL_VERSION} -> ${NEW_VERSION} (base ${BASE_VERSION}, main ${MAIN_VERSION})"
echo "  ${CHART_YAML}"
echo "  ${APP_YAML}"
echo "Commit with: git commit -m \"chore(${CHART_NAME}): bump chart to ${NEW_VERSION}\""
