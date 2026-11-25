#!/usr/bin/env bash
# Tests preview.sh with fake gh, kubectl, pnpm and curl on PATH: it writes
# one PR comment only with a URL the uploader returned, and edits its own
# earlier comment instead of adding another.
set -o errexit -o nounset -o pipefail

script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/preview.sh"
[[ -f "$script" ]] || script="${TEST_SRCDIR}/_main/projects/jomcgi-site/preview/preview.sh"
root="$(mktemp -d "${TEST_TMPDIR:-/tmp}/preview-test.XXXXXX")"
trap 'rm -rf "$root"' EXIT
fail() {
	echo "FAIL: $*" >&2
	exit 1
}

# A checkout with the paths preview.sh reads.
repo="$root/repo"
mkdir -p "$repo/projects/jomcgi-site/web" "$repo/projects/factory-storyboard/fixtures/objects"
echo '{}' >"$repo/projects/factory-storyboard/fixtures/manifest.json"
echo '{}' >"$repo/projects/factory-storyboard/fixtures/objects/a.json"
git -C "$repo" init -q
git -C "$repo" add -A
git -C "$repo" -c user.name=t -c user.email=t@example.com commit -qm init
sha="$(git -C "$repo" rev-parse HEAD)"
other="$(printf 'b%.0s' {1..40})"
stale="$(printf 'c%.0s' {1..40})"

bin="$root/bin"
log="$root/calls.log"
mkdir -p "$bin"
# Bazel passes the pinned node as NODE_BIN; locally PATH's node serves.
node="${NODE_BIN:+$PWD/$NODE_BIN}"
node="${node:-$(command -v node)}"
ln -s "$node" "$bin/node"
cat >"$bin/pnpm" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *build* ]]; then
	mkdir -p projects/jomcgi-site/web/dist/.vite
	echo '<!doctype html>' >projects/jomcgi-site/web/dist/index.html
fi
EOF
cat >"$bin/kubectl" <<'EOF'
#!/usr/bin/env bash
echo "Forwarding from 127.0.0.1:18080 -> 8080"
exec sleep 30
EOF
# The uploader's reply comes from $REPLY_STATUS and $REPLY_BODY.
cat >"$bin/curl" <<'EOF'
#!/usr/bin/env bash
while (($#)); do
	[[ "$1" == -o ]] && out="$2"
	[[ "$1" == --data-binary ]] && cp "${2#@}" "$CALLS.tgz"
	shift
done
printf '%s' "$REPLY_BODY" >"$out"
printf '%s' "$REPLY_STATUS"
EOF
# gh: the PR's head is $PR_HEAD; $EXISTING lists earlier comments as JSON.
# The comment lookup applies preview.sh's --jq filter: the login and the
# marker it names select the ids.
cat >"$bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >>"$CALLS"
case "$1 $2" in
"pr view") echo "{\"number\":42,\"headRefOid\":\"$PR_HEAD\",\"state\":\"OPEN\"}" ;;
"api user") echo jomcgi ;;
"api --paginate")
	node -e '
		const filter = process.argv[1];
		const login = filter.match(/\.user\.login == "([^"]*)"/)[1];
		const marker = filter.match(/startswith\("([^"]*)"\)/)[1];
		for (const c of JSON.parse(process.env.EXISTING)) {
			if (c.user.login === login && c.body.startsWith(marker)) console.log(c.id);
		}
	' "${*: -1}"
	;;
esac
EOF
chmod +x "$bin"/*

run() {
	(cd "$repo" && CALLS="$log" PATH="$bin:$PATH" XDG_CACHE_HOME="$root/cache" \
		PREVIEW_KUBECONFIG=/dev/null bash "$script") >"$root/out" 2>&1
}
reset() {
	: >"$log"
	export PR_HEAD="$sha" EXISTING="[]" REPLY_STATUS=200
	export REPLY_BODY="{\"url\":\"https://pr-42-jomcgi-site-dev.example.workers.dev/\",\"sha\":\"$sha\"}"
}

# 1. No earlier comment: one comment is created with the URL and SHA.
reset
run || fail "preview.sh failed: $(cat "$root/out")"
grep -q "preview: https://pr-42-jomcgi-site-dev.example.workers.dev/" "$root/out" || fail "no URL printed"
grep -q "api -X POST repos/jomcgi-org/homelab/issues/42/comments" "$log" || fail "no comment created"
grep -q -- "-f body=<!-- jomcgi-site-preview -->" "$log" || fail "comment lacks the marker"
grep -q "$sha" "$log" || fail "comment lacks the SHA"
grep -q "PATCH" "$log" && fail "created and edited"
tar -tzf "$log.tgz" | grep -q '^\./data/factory/manifest\.json$' || fail "fixtures missing from the archive"
tar -tzf "$log.tgz" | grep -q '\.vite' && fail "build internals in the archive"

# 2. Its own marked comment exists: that one is edited. Someone else's
#    marked comment is left alone.
reset
export EXISTING='[{"id":5,"user":{"login":"someone"},"body":"<!-- jomcgi-site-preview --> x"},
	{"id":9,"user":{"login":"jomcgi"},"body":"<!-- jomcgi-site-preview --> old"}]'
run || fail "preview.sh failed: $(cat "$root/out")"
grep -q "api -X PATCH repos/jomcgi-org/homelab/issues/comments/9" "$log" || fail "own comment not edited"
grep -q "POST" "$log" && fail "a second comment was created"

# 3. The uploader refuses: the error is shown and nothing is posted.
reset
export REPLY_STATUS=503 REPLY_BODY='{"error":"Cloudflare Access does not protect the host"}'
run && fail "a refusal passed"
grep -q "refused (HTTP 503): Cloudflare Access does not protect" "$root/out" || fail "refusal not shown"
grep -q "api -X" "$log" && fail "a comment was written after a refusal"

# 4. A URL for another PR, or another SHA, is never posted.
reset
export REPLY_BODY="{\"url\":\"https://evil.example/\",\"sha\":\"$sha\"}"
run && fail "an unexpected URL passed"
grep -q "api -X" "$log" && fail "posted an unexpected URL"
reset
export REPLY_BODY="{\"url\":\"https://pr-42-jomcgi-site-dev.example.workers.dev/\",\"sha\":\"$other\"}"
run && fail "another SHA passed"
grep -q "api -X" "$log" && fail "posted another SHA"

# 5. HEAD is not the PR head: refused before the build.
reset
export PR_HEAD="$stale"
run && fail "a stale HEAD passed"
grep -q "push first" "$root/out" || fail "no push hint"
[[ -e "$repo/projects/jomcgi-site/web/dist" ]] && rm -rf "$repo/projects/jomcgi-site/web/dist"

echo PASS
