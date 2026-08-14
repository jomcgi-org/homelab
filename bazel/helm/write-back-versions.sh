#!/usr/bin/env bash
# write-back-versions.sh: commit the versions main just published back to main.
#
# ADR platform/009 decision 1 makes the chart version an output of merging. The
# publish step (bazel/helm/push.sh.tpl) computes the version, publishes that
# chart, and records "<chart_dir> <version>" to one file per chart in
# CHART_VERSION_RECORD_DIR. This reads those records and writes Chart.yaml
# `version:` and deploy/application.yaml `targetRevision:` back to main in ONE
# commit.
#
# Why this is a separate step and not part of the publish: push_all is a
# multirun with `jobs = 0`, so ~24 chart pushes run concurrently in a single
# workspace. Committing from inside one would race the others on the git index,
# and 24 concurrent pushes would race main's ref and mostly lose. Recording to
# per-chart files has no shared state at all, and the serial resource (main's
# ref) is then touched exactly once.
#
# TWO PROPERTIES MAKE THIS SAFE UNDER CONCURRENT MAIN RUNS. BuildBuddy exempts
# the default branch from superseded-run cancellation, so two merges in quick
# succession run two of these.
#
#   1. Versions are commit-derived (chart-version.sh), so concurrent runs
#      compute DIFFERENT versions rather than colliding on one.
#   2. The write-back is monotonic: it refuses to lower a version, and retries
#      on top of whatever landed first. Concurrent runs therefore converge on
#      the highest version, which is the newest commit, instead of the last job
#      to finish winning. Without this a slow publish for an older commit could
#      walk targetRevision BACKWARDS and silently roll production back.
#
# Usage: write-back-versions.sh <record_dir>
# Env:
#   WRITE_BACK_TRIES  push attempts before giving up (default 5)
#   WRITE_BACK_DRY_RUN  if non-empty, print what would change and do not push
set -o errexit -o nounset -o pipefail

RECORD_DIR="${1:?Usage: write-back-versions.sh <record_dir>}"
TRIES="${WRITE_BACK_TRIES:-5}"
DRY_RUN="${WRITE_BACK_DRY_RUN:-}"

if [[ ! -d "$RECORD_DIR" ]]; then
	echo "No record directory at ${RECORD_DIR}; nothing was published, nothing to write back."
	exit 0
fi

shopt -s nullglob
RECORDS=("$RECORD_DIR"/*)
shopt -u nullglob
if [[ "${#RECORDS[@]}" -eq 0 ]]; then
	echo "No published charts recorded; nothing to write back."
	exit 0
fi

# A targetRevision line pinning an OCI chart version, as opposed to a git ref.
# Anchored end to end so `targetRevision: HEAD` and `targetRevision: main` can
# never match.
SEMVER_TR_RE='^[[:space:]]*targetRevision:[[:space:]]*"?[0-9]+\.[0-9]+\.[0-9]+"?[[:space:]]*$'

# Numeric semver comparison. `sort -V` is not enough on its own: it happily
# reports equality-ish orderings for strings that are not versions at all, so
# the caller checks the parsed form first.
_is_semver() {
	[[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# Print the greater of two semvers.
_max_version() {
	printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1
}

# Locate the ArgoCD Application for a chart dir, using the two layout
# conventions in this repo: projects/<svc>/chart with a sibling deploy/, or a
# chart directory with application.yaml colocated.
_app_yaml_for() {
	local chart_dir="$1" candidate
	candidate="$(dirname "$chart_dir")/deploy/application.yaml"
	if [[ -f "$candidate" ]]; then
		echo "$candidate"
		return 0
	fi
	if [[ -f "${chart_dir}/application.yaml" ]]; then
		echo "${chart_dir}/application.yaml"
		return 0
	fi
	echo ""
}

# Further Applications whose chart pin this bot maintains, beyond the production
# one _app_yaml_for finds. One per line, empty if none.
#
# OPT-IN BY MARKER, NOT BY PATH CONVENTION, and the distinction is the whole
# point. `projects/<svc>/dev/deploy/application.yaml` is not uniformly ours to
# write: Kargo owns monolith-dev's targetRevision at runtime (#4744), and the
# value committed there is a deliberately frozen bootstrap floor that drifts
# from the live one on purpose. A path rule matching `*/dev/deploy/` would
# rewrite that floor on every publish, fighting the promotion pipeline and
# destroying the revert lever the committed value exists to be. So an
# Application joins the write-back only by saying so in its own file.
#
# A dev Application that opts in catches up in ONE jump the next time its chart
# publishes, rather than walking forward one version at a time, because the
# version written is whatever main just published.
MANAGED_MARKER='chart-version-bot: manage-target-revision'

_opted_in_app_yamls_for() {
	local chart_dir="$1" svc_dir candidate
	svc_dir="$(dirname "$chart_dir")"
	shopt -s nullglob
	for candidate in "${svc_dir}"/*/deploy/application.yaml; do
		if [[ -f "$candidate" ]] && grep -qF "$MANAGED_MARKER" "$candidate"; then
			echo "$candidate"
		fi
	done
	shopt -u nullglob
	return 0
}

# Point one Application's semver targetRevision at `version`, staging it if it
# moved. Split out of the main loop so production and every opted-in
# Application go through the SAME anchoring and verification, rather than the
# dev path getting a looser copy of it.
_rewrite_target_revision() {
	local app_yaml="$1" version="$2" tr_count old_tr
	# Match ONLY a semver targetRevision. deploy/application.yaml is a
	# multi-source Application: the chart source is pinned to a version,
	# and the $values source is a GIT REF (`targetRevision: HEAD`).
	# A loose `grep targetRevision: | head -1` would eventually rewrite
	# that git ref into a chart version and break the values source, so
	# the pattern is anchored to a semver and the count is asserted.
	# This mirrors bump-chart.sh, which got it right first.
	tr_count=$(grep -cE "$SEMVER_TR_RE" "$app_yaml" || true)
	if [[ "$tr_count" -ne 1 ]]; then
		echo "WARNING: ${app_yaml} has ${tr_count} semver targetRevision line(s), expected 1; leaving it alone." >&2
		return 0
	fi
	old_tr=$(grep -E "$SEMVER_TR_RE" "$app_yaml" | head -1 | awk '{print $2}' | tr -d '"')
	if [[ "$old_tr" == "$version" ]]; then
		return 0
	fi
	sed "s/targetRevision: ${old_tr}\$/targetRevision: ${version}/" "$app_yaml" >"${app_yaml}.tmp"
	mv "${app_yaml}.tmp" "$app_yaml"
	if ! grep -qF "targetRevision: ${version}" "$app_yaml"; then
		echo "ERROR: failed to rewrite targetRevision in ${app_yaml} (unusual formatting?)." >&2
		exit 1
	fi
	git add "$app_yaml"
}

git config user.name "chart-version-bot"
git config user.email "chart-version-bot@users.noreply.github.com"

attempt=1
while [[ "$attempt" -le "$TRIES" ]]; do
	# Re-read main every attempt. On a retry another run's write-back has landed,
	# and the monotonic check below has to see ITS versions, not the ones this
	# process read before losing the race.
	git fetch --quiet origin main
	git checkout --quiet -B write-back-main origin/main

	CHANGED=0
	SUMMARY=()
	for record in "${RECORDS[@]}"; do
		read -r CHART_DIR VERSION <"$record"
		[[ -z "${CHART_DIR:-}" || -z "${VERSION:-}" ]] && continue

		CHART_YAML="${CHART_DIR}/Chart.yaml"
		[[ -f "$CHART_YAML" ]] || continue

		ON_MAIN=$(grep '^version:' "$CHART_YAML" | head -1 | awk '{print $2}' | tr -d '"')
		if ! _is_semver "$VERSION" || ! _is_semver "$ON_MAIN"; then
			echo "WARNING: skipping ${CHART_DIR}: unparseable version (main '${ON_MAIN}', published '${VERSION}')." >&2
			continue
		fi

		# Monotonic: never lower a version. A concurrent run that published a
		# HIGHER version has already written it, and overwriting it here would
		# roll the deployed chart backwards.
		if [[ "$(_max_version "$ON_MAIN" "$VERSION")" != "$VERSION" ]] || [[ "$ON_MAIN" == "$VERSION" ]]; then
			echo "main is at ${ON_MAIN} for ${CHART_DIR}; not writing ${VERSION}."
			continue
		fi

		sed "s/^version:.*/version: ${VERSION}/" "$CHART_YAML" >"${CHART_YAML}.tmp"
		mv "${CHART_YAML}.tmp" "$CHART_YAML"
		git add "$CHART_YAML"

		APP_YAML="$(_app_yaml_for "$CHART_DIR")"
		if [[ -n "$APP_YAML" ]]; then
			_rewrite_target_revision "$APP_YAML" "$VERSION"
		fi

		# Opted-in Applications (a dev environment tracking the same chart).
		# Read into an array first: _rewrite_target_revision runs `git add`, and
		# piping the loop's input from a process substitution while git writes
		# the index has bitten this repo before.
		OPTED_IN=()
		while IFS= read -r extra_yaml; do
			[[ -n "$extra_yaml" ]] && OPTED_IN+=("$extra_yaml")
		done < <(_opted_in_app_yamls_for "$CHART_DIR")
		for extra_yaml in "${OPTED_IN[@]+"${OPTED_IN[@]}"}"; do
			_rewrite_target_revision "$extra_yaml" "$VERSION"
		done

		SUMMARY+=("${CHART_DIR}: ${ON_MAIN} -> ${VERSION}")
		CHANGED=$((CHANGED + 1))
	done

	if [[ "$CHANGED" -eq 0 ]]; then
		echo "Nothing to write back; main already carries every published version."
		rm -rf "$RECORD_DIR"
		exit 0
	fi

	printf 'Writing back %d chart version(s):\n' "$CHANGED"
	printf '  %s\n' "${SUMMARY[@]}"

	if [[ -n "$DRY_RUN" ]]; then
		echo "WRITE_BACK_DRY_RUN set; not committing or pushing."
		exit 0
	fi

	# The commit touches ONLY Chart.yaml and application.yaml, never templates,
	# so the next publish rebuilds identical image digests and does nothing.
	# That, plus the chart-version-bot author check in push.sh.tpl, is the loop
	# guard.
	git commit --quiet -m "chore(charts): publish ${CHANGED} chart version(s)

$(printf '%s\n' "${SUMMARY[@]}")

Written back by write-back-versions.sh (ADR platform/009 decision 1)."

	# `|| PUSH_RC=$?` keeps errexit from aborting on the very failure this loop
	# exists to handle, and captures the message so the two causes below can be
	# told apart.
	PUSH_RC=0
	PUSH_ERR=$(git push --quiet origin HEAD:main 2>&1) || PUSH_RC=$?
	if [[ "$PUSH_RC" -eq 0 ]]; then
		echo "Write-back pushed on attempt ${attempt}."
		rm -rf "$RECORD_DIR"
		exit 0
	fi
	[[ -n "$PUSH_ERR" ]] && echo "$PUSH_ERR" >&2

	# Distinguish a LOSABLE RACE from a PERMANENT REFUSAL.
	#
	# Retrying is the right answer to the race this loop was written for:
	# another publish landed first, so the rebase at the top of the loop wins
	# the next attempt. A repository rule violation is not that. It is the same
	# answer every time, and calling it "another publish landed first" is how a
	# permission problem spent five attempts wearing a race's clothes on
	# 2026-08-10, burning the retries and reporting the wrong cause.
	if grep -qE 'GH013|Repository rule violations|protected branch|refusing to allow' <<<"$PUSH_ERR"; then
		{
			echo "ERROR: main refused the write-back on a repository rule, which retrying cannot fix."
			echo "The identity pushing this must be able to bypass the ruleset on refs/heads/main."
			echo "The charts ARE published; main's Chart.yaml just does not reference them yet."
		} >&2
		exit 1
	fi

	echo "Push rejected (another publish landed first); retrying (${attempt}/${TRIES})." >&2
	attempt=$((attempt + 1))
done

echo "ERROR: could not write chart versions back to main after ${TRIES} attempts." >&2
echo "The charts ARE published; main's Chart.yaml just does not reference them yet," >&2
echo "so ArgoCD will keep deploying the previous version until this is resolved." >&2
exit 1
