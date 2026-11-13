#!/usr/bin/env bash
# Candidate library, deliberately not sourced by buildbuddy.yaml.
# Arguments and environment are untrusted routing data, never authorization.

candidate_role() {
	local event="${1:-}" pr="${2:-}" branch="${3:-}"
	case "$event:$pr:$branch" in
	pull_request:*:*)
		[[ "$pr" =~ ^[1-9][0-9]*$ && -n "$branch" ]] || return 2
		echo pr
		;;
	push:0:gh-readonly-queue/?*) echo queue ;;
	push:0:main) echo main ;;
	*) return 2 ;;
	esac
}

candidate_pr_login() {
	# Only the future provider adapter may call this in a restricted PR lane.
	# This diagnostic cannot detect arbitrary injected secrets or saved state.
	[[ "${1:-}" == pr ]] || return 2
	if [[ -n "${GHCR_TOKEN:-}" || -n "${DOCKERHUB_TOKEN:-}" ||
		-n "${REPO_TOKEN:-}" || -n "${GITHUB_TOKEN:-}" || -n "${GH_TOKEN:-}" ]]; then
		echo 'Restricted PR mode received a forbidden credential input' >&2
		return 1
	fi
	[[ -n "${GHCR_PULL_TOKEN:-}" ]] || {
		echo 'Restricted PR mode requires GHCR_PULL_TOKEN; no write-token fallback' >&2
		return 1
	}
	# The provider must supply an empty, isolated Docker config directory.
	[[ -n "${DOCKER_CONFIG:-}" && -d "$DOCKER_CONFIG" &&
		-z "$(ls -A "$DOCKER_CONFIG")" ]] || return 1
	printf '%s\n' "$GHCR_PULL_TOKEN" | docker login ghcr.io -u jomcgi --password-stdin
}

candidate_pr_drift() {
	[[ "${1:-}" == pr ]] || return 2
	shift
	# Arguments are newly generated files from prepare_go_build_graph.
	if git diff --exit-code && [[ $# == 0 ]]; then
		echo 'All files formatted correctly'
		return 0
	fi
	echo "Restricted PR drift: run 'ci lint' and 'ci regen', generate BUILD files with the pinned Linux Gazelle, and commit the repairs on the source branch." >&2
	git --no-pager diff --stat
	return 1
}
