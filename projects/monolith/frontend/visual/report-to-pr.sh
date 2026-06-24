#!/usr/bin/env bash
# report-to-pr.sh: upload changed visual PNGs as release assets and post/update
# an inline before/after/diff PR comment. Mirrors bazel/helm/ci-diff-manifests.sh.
#
# Images are hosted as GitHub release assets on a single long-lived prerelease
# tag (visual-snapshots). The repo is public, so the asset browser_download_url
# renders inline in the comment. One idempotent comment per PR, found/updated by
# a marker, exactly like the Helm manifest-diff comment.
#
# Requires (PR builds only): BUILDBUDDY_PULL_REQUEST_NUMBER (set by BuildBuddy),
#           GHCR_TOKEN (gh auth + GitHub API). Both absent => prints to stdout.
set -euo pipefail

MARKER="<!-- public-visual-regression -->"
TAG="visual-snapshots"
REPO_ROOT="$(git rev-parse --show-toplevel)"
VDIR="$REPO_ROOT/projects/monolith/frontend/visual"
PR_NUMBER="${BUILDBUDDY_PULL_REQUEST_NUMBER:-}"
SHA="$(git rev-parse --short HEAD)"

export GH_TOKEN="${GHCR_TOKEN:-}"
GH="$(command -v gh)"

report="$VDIR/out/report.json"
changed=$(jq -r '.changed[]' "$report")
added=$(jq -r '.added[]' "$report")

# Guard BEFORE any release-asset side effects: uploads and comments only make
# sense on a PR build with a token. Without both (local run, push to main, no
# token), print a summary and exit inert so ambient gh auth is never used to
# touch the repo's releases.
if [ -z "$PR_NUMBER" ] || [ -z "$GH_TOKEN" ]; then
	echo "No PR number or token; skipping upload/comment."
	echo "changed: ${changed:-none}"
	echo "added: ${added:-none}"
	exit 0
fi

# Look up an existing marker comment up front (read-only) so we can decide
# whether a no-change run should stay silent or clear a stale comment.
EXISTING=$("$GH" api "repos/{owner}/{repo}/issues/${PR_NUMBER}/comments" --paginate \
	--jq ".[] | select(.body | startswith(\"$MARKER\")) | .id" 2>/dev/null | head -1) || true

if [ -z "$changed$added" ]; then
	# No differences: do not create a NEW comment, so PRs that do not touch the
	# frontend stay clean. But if a prior run flagged changes, clear that comment
	# so it does not dangle with stale results.
	if [ -z "$EXISTING" ]; then
		echo "No visual changes and no existing comment; nothing to post."
		exit 0
	fi
	BODY="${MARKER}
## Public page visual diff
No visual changes across the public pages. ✅ (Previously flagged changes are resolved.)"
else
	# Ensure the hosting prerelease exists (idempotent). The trailing || true
	# absorbs the TOCTOU race where two concurrent PR builds both see the
	# release absent and both try to create it; the loser's "already exists"
	# error is benign since the release exists either way.
	"$GH" release view "$TAG" >/dev/null 2>&1 ||
		"$GH" release create "$TAG" --prerelease --title "Visual snapshots" \
			--notes "Auto-managed asset host for public-page visual regression. Do not delete." ||
		true

	# Resolve owner/repo once for the public download URL.
	NWO="$("$GH" repo view --json nameWithOwner -q .nameWithOwner)"

	# Staging dir for uniquely-named asset copies. `gh release upload file#label`
	# only sets a DISPLAY label; the asset's download URL uses the file's
	# BASENAME. So stage each file under its unique asset name and upload that,
	# making basename == asset name == the URL we embed.
	STAGE="$VDIR/out/_assets"
	mkdir -p "$STAGE"

	upload() { # name file -> echoes the public asset URL
		local name="$1" file="$2" asset="pr${PR_NUMBER}-${SHA}-${name}.png"
		cp "$file" "$STAGE/$asset"
		"$GH" release upload "$TAG" "$STAGE/$asset" --clobber >/dev/null
		echo "https://github.com/${NWO}/releases/download/${TAG}/${asset}"
	}

	rows=""
	# Changed pages render on both main (before) and the PR (after). "before" is
	# main's render, staged by the workflow into out/baseline/; "after" is the PR
	# render in out/. A pixel diff image exists only when dimensions matched; a
	# height/width change (e.g. text reflow on a fullPage shot) is reported as
	# changed with no diff PNG, so the diff cell must be guarded or the
	# missing-file upload aborts the script.
	for name in $changed; do
		before=$(upload "${name}-before" "$VDIR/out/baseline/${name}.png")
		after=$(upload "${name}-after" "$VDIR/out/${name}.png")
		diff_file="$VDIR/out/diff/${name}.png"
		if [ -f "$diff_file" ]; then
			diff_url=$(upload "${name}-diff" "$diff_file")
			diff_cell="<img src=\"${diff_url}\" width=\"260\">"
		else
			diff_cell="(dimensions changed)"
		fi
		rows="${rows}
<details><summary><b>${name}</b> (changed)</summary>

| before | after | diff |
|---|---|---|
| <img src=\"${before}\" width=\"260\"> | <img src=\"${after}\" width=\"260\"> | ${diff_cell} |

</details>"
	done
	# Added pages exist on the PR but not on main (a brand-new public page), so
	# there is no "before" render; only the current capture is uploaded.
	for name in $added; do
		after=$(upload "${name}-after" "$VDIR/out/${name}.png")
		rows="${rows}
<details><summary><b>${name}</b> (new, no baseline)</summary>

<img src=\"${after}\" width=\"320\">

</details>"
	done

	# grep -c on an empty string exits 1 under pipefail; the || true keeps the
	# count at 0 without aborting the script.
	n_changed=$(printf '%s' "$changed" | grep -c . || true)
	n_added=$(printf '%s' "$added" | grep -c . || true)
	BODY="${MARKER}
## Public page visual diff
Changed: **${n_changed}**, New: **${n_added}**, commit \`${SHA}\` (vs \`origin/main\`)

These are the pixel differences this PR introduces against the current \`main\` render. There is nothing to accept: once the PR merges, main's render becomes the new reference automatically.
${rows}"
fi

if [ -n "$EXISTING" ]; then
	"$GH" api "repos/{owner}/{repo}/issues/comments/${EXISTING}" --method PATCH --field body="$BODY" --silent
else
	"$GH" pr comment "$PR_NUMBER" --body "$BODY"
fi
echo "Posted visual diff comment."
