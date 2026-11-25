#!/usr/bin/env bash
# Builds jomcgi.dev, serves it on loopback with the invented factory fixtures
# and takes screenshots at phone and desktop widths in light and dark
# (README "Local preview", HOME-877). Nothing is hosted or committed.
#
# Usage: projects/jomcgi-site/preview/preview.sh [options] [/page ...]
#   --out DIR   The one output directory: PNGs, index.md and site/ (the
#               built site). Each run clears it first. It must be outside
#               the repository or ignored by git.
#               Default: projects/jomcgi-site/preview/out (git ignores it).
#   --no-build  Use the existing web/dist; skip pnpm install and build.
#   --comment   Also write one PR comment with the page results. It edits
#               this account's earlier preview comment on the PR, if any.
#   --pr N      The PR for --comment. Default: the PR of the current branch.
# Exit 0: every page loaded clean. 1: a page or the build failed.
# 2: bad arguments or output directory. 130/143: stopped by a signal.
set -o errexit -o nounset -o pipefail

usage() { sed -n '2,17s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; }
die() {
	echo "preview: $1" >&2
	exit "${2:-1}"
}

out="" build=1 comment=0 pr="" pages=()
while (($#)); do
	case "$1" in
	--out)
		[[ -n "${2:-}" ]] || die "--out needs a directory" 2
		out="$2"
		shift
		;;
	--pr)
		[[ "${2:-}" =~ ^[0-9]+$ ]] || die "--pr needs a PR number" 2
		pr="$2"
		shift
		;;
	--no-build) build=0 ;;
	--comment) comment=1 ;;
	-h | --help) usage && exit 0 ;;
	-*) usage >&2 && die "unknown option $1" 2 ;;
	*) pages+=("$1") ;;
	esac
	shift
done

# Canonical paths, so ".." or a symlink cannot hide the repository.
repo="$(realpath -- "$(git rev-parse --show-toplevel)")"
out="$(realpath -m -- "${out:-$repo/projects/jomcgi-site/preview/out}")"
out="${out%/}"
[[ -n "$out" ]] || die "--out cannot be /" 2
cd "$repo"
tools="${XDG_CACHE_HOME:-$HOME/.cache}/homelab-tools/usr/bin"
[[ -d "$tools" ]] && PATH="$tools:$PATH"
sha="$(git rev-parse HEAD)"

# Output never enters git: a directory inside the repository must be ignored.
# A run clears only a directory this tool made (it holds the marker file).
marker_file=.jomcgi-site-preview
if [[ "$out/" == "$repo/"* ]] && ! git check-ignore -q "$out/"; then
	die "--out $out is inside the repository and not ignored by git" 2
fi
if [[ -e "$out" && ! -e "$out/$marker_file" ]] &&
	[[ ! -d "$out" || -n "$(ls -A "$out")" ]]; then
	die "--out $out is not empty and was not made by preview.sh; choose another" 2
fi

# A comment names the PR head SHA, so the screenshots must be of that SHA.
# The build writes untracked files of its own; only tracked files must match.
if ((comment)); then
	[[ -z "$(git status --porcelain --untracked-files=no)" ]] ||
		die "--comment needs a clean checkout; commit and push first"
	read -r pr head < <(gh pr view ${pr:+"$pr"} --json number,headRefOid \
		--jq '"\(.number) \(.headRefOid)"') ||
		die "no pull request for this branch; open it first or pass --pr"
	[[ "$head" == "$sha" ]] || die "PR #$pr head is $head but HEAD is $sha; push first"
fi

rm -rf "$out"
mkdir -p "$out"
touch "$out/$marker_file"

web=projects/jomcgi-site/web
if ((build)); then
	echo "preview: building $sha"
	if ! { pnpm install --frozen-lockfile --ignore-scripts --reporter=silent &&
		pnpm --dir "$web" build; } >"$out/build.log" 2>&1; then
		tail -40 "$out/build.log" >&2
		die "the site build failed; see $out/build.log"
	fi
fi
[[ -f "$web/dist/index.html" ]] || die "no built site in $web/dist"
cp -R "$web/dist" "$out/site"

# The screenshot step owns the loopback server and the browser. It runs as a
# tracked child, so every exit path, Ctrl-C included, stops it.
child=""
# shellcheck disable=SC2329 # Called by the EXIT trap.
stop_child() {
	if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
		kill -TERM "$child" 2>/dev/null || true
		wait "$child" 2>/dev/null || true
	fi
}
trap stop_child EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
status=0
node "$web/preview-shots.mjs" --dist "$out/site" \
	--fixtures projects/factory-storyboard/fixtures --out "$out" \
	${pages[@]+"${pages[@]}"} &
child=$!
wait "$child" || status=$?
child=""
if ((status == 2)); then exit 2; fi
[[ -f "$out/index.md" ]] || die "no screenshots were written"
echo "preview: screenshots in $out"

if ((comment)); then
	# GitHub cannot take images through the API: the comment is text only.
	marker="<!-- jomcgi-site-preview -->"
	body="$marker
**jomcgi.dev local preview** of \`$sha\` with the invented factory fixtures.
Phone (390 px) and desktop (1280 px), light and dark. The screenshots stay
with the agent that ran \`projects/jomcgi-site/preview/preview.sh\`.

$(sed -n '/^## Pages/,/^## Screenshots/{/^## /d;p}' "$out/index.md")"
	repository="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
	login="$(gh api user --jq .login)" || die "gh has no signed-in account"
	id="$(gh api --paginate "repos/$repository/issues/$pr/comments?per_page=100" \
		--jq ".[] | select(.user.login == \"$login\" and (.body | startswith(\"$marker\"))) | .id" | sed -n 1p)"
	if [[ -n "$id" ]]; then
		gh api -X PATCH "repos/$repository/issues/comments/$id" -f body="$body" >/dev/null
		echo "preview: PR #$pr comment updated"
	else
		gh api -X POST "repos/$repository/issues/$pr/comments" -f body="$body" >/dev/null
		echo "preview: PR #$pr comment created"
	fi
fi
exit "$status"
