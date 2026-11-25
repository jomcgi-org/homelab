#!/usr/bin/env bash
# Builds jomcgi.dev with the invented factory fixtures and uploads it as the
# preview of this branch's PR (README "PR previews"). Prints the preview URL.
#
# Usage, from anywhere in a clean checkout whose HEAD is pushed as a PR head:
#   projects/jomcgi-site/preview/preview.sh [PR number]
#
# The upload goes through `kubectl port-forward` to the uploader pod in
# namespace jomcgi-site-dev; the session holds no Cloudflare or GitHub
# credential. PREVIEW_KUBECONFIG overrides the default sessions kubeconfig.
set -o errexit -o nounset -o pipefail

repo="$(git rev-parse --show-toplevel)"
cd "$repo"
export KUBECONFIG="${PREVIEW_KUBECONFIG:-$HOME/.kube/factory-sessions.yaml}"
tools="${XDG_CACHE_HOME:-$HOME/.cache}/homelab-tools/usr/bin"
[[ -d "$tools" ]] && PATH="$tools:$PATH"

die() {
	echo "preview: $*" >&2
	exit 1
}

# The build writes untracked files of its own (web/docs-manifest.json,
# web/docs/), so only tracked files must match HEAD.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
	die "the checkout has uncommitted changes; commit and push, then run again"
fi
sha="$(git rev-parse HEAD)"
if ! pr_json="$(gh pr view ${1:+"$1"} --json number,headRefOid,state 2>/dev/null)"; then
	die "no pull request for this branch; push it and open the PR first, then run again"
fi
read -r pr head state < <(node -e '
	const p = JSON.parse(process.argv[1]);
	console.log(p.number, p.headRefOid, p.state);
' "$pr_json")
[[ "$state" == OPEN ]] || die "PR #$pr is $state, not open"
[[ "$head" == "$sha" ]] || die "PR #$pr head is $head but HEAD is $sha; push first"

work="$(mktemp -d)"
forward=""
cleanup() {
	[[ -n "$forward" ]] && kill "$forward" 2>/dev/null
	rm -rf "$work"
}
trap cleanup EXIT

echo "preview: building $sha for PR #$pr"
pnpm install --frozen-lockfile --ignore-scripts --reporter=silent
pnpm --dir projects/jomcgi-site/web build >"$work/build.log" 2>&1 ||
	{
		tail -40 "$work/build.log" >&2
		die "the site build failed"
	}

# The deployable files only: .vite and .trips-renderer are build internals.
# The fixtures are the ones Vite serves in development (web/factory-fixtures.ts).
site="$work/site"
cp -R projects/jomcgi-site/web/dist "$site"
rm -rf "$site/.vite" "$site/.trips-renderer"
mkdir -p "$site/data/factory"
cp -R projects/factory-storyboard/fixtures/manifest.json \
	projects/factory-storyboard/fixtures/objects "$site/data/factory/"
tar --format=pax --numeric-owner --owner=0 --group=0 -C "$site" -czf "$work/site.tgz" .

kubectl -n jomcgi-site-dev port-forward pod/jomcgi-site-preview-0 :8080 >"$work/forward.log" 2>&1 &
forward=$!
port=""
for _ in $(seq 1 50); do
	port="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) .*/\1/p' "$work/forward.log" | head -1)"
	[[ -n "$port" ]] && break
	kill -0 "$forward" 2>/dev/null || break
	sleep 0.2
done
[[ -n "$port" ]] || {
	cat "$work/forward.log" >&2
	die "could not port-forward to the uploader (is it enabled, and is KUBECONFIG a sessions identity?)"
}

echo "preview: uploading (this takes about a minute)"
status="$(curl -sS -o "$work/reply.json" -w '%{http_code}' --max-time 900 \
	-X POST -H 'Content-Type: application/gzip' --data-binary "@$work/site.tgz" \
	"http://127.0.0.1:$port/previews?pr=$pr&sha=$sha")" || die "the upload request failed"
# shellcheck disable=SC2016 # JavaScript template literals, not shell.
node -e '
	const reply = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
	if (process.argv[2] !== "200") {
		console.error(`preview: refused (HTTP ${process.argv[2]}): ${reply.error}`);
		process.exit(1);
	}
	console.log(`preview: ${reply.url}`);
	console.log(`preview: built from ${reply.sha}; PR comment ${reply.comment}`);
' "$work/reply.json" "$status"
