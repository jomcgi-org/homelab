#!/usr/bin/env bash
# Tests preview.sh with fake pnpm, node and gh on PATH: argument handling,
# exit codes, the one cleared output directory, no process left after a
# signal, no output in git, and one PR comment that is edited, never
# duplicated.
set -o errexit -o nounset -o pipefail

script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/preview.sh"
[[ -f "$script" ]] || script="${TEST_SRCDIR}/_main/projects/jomcgi-site/preview/preview.sh"
root="$(mktemp -d "${TEST_TMPDIR:-/tmp}/preview-test.XXXXXX")"
trap 'rm -rf "$root"' EXIT
fail() {
	echo "FAIL: $*" >&2
	cat "$root/out" >&2 || true
	exit 1
}

repo="$root/repo"
mkdir -p "$repo/projects/jomcgi-site/web"
echo x >"$repo/tracked"
# The repository's own ignore line for the default output directory.
gitignore="${TEST_SRCDIR:+$TEST_SRCDIR/_main/.gitignore}"
gitignore="${gitignore:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)/.gitignore}"
grep -x "projects/jomcgi-site/preview/out/" "$gitignore" >"$repo/.gitignore" ||
	fail "the repository .gitignore does not ignore the default output directory"
printf 'dist/\nignored/\n' >>"$repo/.gitignore"
git -C "$repo" init -q
git -C "$repo" add -A
git -C "$repo" -c user.name=t -c user.email=t@example.com commit -qm init
sha="$(git -C "$repo" rev-parse HEAD)"

bin="$root/bin"
calls="$root/calls.log"
mkdir -p "$bin"
cat >"$bin/pnpm" <<'EOF'
#!/usr/bin/env bash
echo "pnpm $*" >>"$CALLS"
if [[ "$*" == *build* ]]; then
	mkdir -p projects/jomcgi-site/web/dist
	echo '<!doctype html>' >projects/jomcgi-site/web/dist/index.html
fi
EOF
# The screenshot step: writes a PNG and index.md and exits with $SHOTS_EXIT.
# With $SHOTS_HANG it records its PID and waits, as the server would.
cat >"$bin/node" <<'EOF'
#!/usr/bin/env bash
echo "node $*" >>"$CALLS"
while (($#)); do
	[[ "$1" == --out ]] && out="$2"
	[[ "$1" == --dist ]] && dist="$2"
	shift
done
[[ -f "$dist/index.html" ]] || exit 2
if [[ -n "${SHOTS_HANG:-}" ]]; then
	echo $$ >"$CALLS.pid"
	exec sleep 300
fi
if [[ "$SHOTS_EXIT" != 2 ]]; then
	touch "$out/home-phone-light.png"
	printf '# x\n\n## Pages\n\n| Page | Result | Details |\n| `/` | %s | |\n\n## Screenshots\n\n| row |\n' \
		"$([[ "$SHOTS_EXIT" == 0 ]] && echo ok || echo FAIL)" >"$out/index.md"
fi
exit "$SHOTS_EXIT"
EOF
# gh: the PR head is $PR_HEAD; $EXISTING lists comment ids by this account
# that start with the marker. Writes record the body in $CALLS.body.
cat >"$bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >>"$CALLS"
case "$1 $2" in
"pr view") echo "42 $PR_HEAD" ;;
"repo view") echo owner/repo ;;
"api user") echo me ;;
"api --paginate")
	[[ "$*" == *'.user.login == "me"'*'startswith("<!-- jomcgi-site-preview -->")'* ]] && printf '%s\n' $EXISTING
	;;
"api -X") for arg; do [[ "$arg" == body=* ]] && printf '%s' "${arg#body=}" >"$CALLS.body"; done ;;
esac
EOF
chmod +x "$bin"/*

export CALLS="$calls" PATH="$bin:$PATH" XDG_CACHE_HOME="$root/cache" TMPDIR="$root"
export SHOTS_EXIT=0 PR_HEAD="$sha" EXISTING=""
run() {
	: >"$calls"
	rm -f "$calls.body"
	(cd "$repo" && bash "$script" "$@") >"$root/out" 2>&1
}
expect_exit() {
	local want="$1" got=0
	shift
	run "$@" || got=$?
	[[ "$got" == "$want" ]] || fail "$*: exit $got, not $want"
}
default="$repo/projects/jomcgi-site/preview/out"

# Arguments.
expect_exit 0 --help
grep -q "Usage:" "$root/out" || fail "--help shows no usage"
expect_exit 2 --bogus
expect_exit 2 --out
expect_exit 2 --pr
expect_exit 2 --pr x
[[ -s "$calls" ]] && fail "a bad argument ran a step"

# Default run: build, then the default pages into the one fixed directory,
# which holds the built site. Nothing appears in git.
expect_exit 0
grep -q "pnpm install --frozen-lockfile" "$calls" || fail "no install"
grep -q "pnpm --dir projects/jomcgi-site/web build" "$calls" || fail "no build"
grep -q -- "--dist $default/site .* --out $default\$" "$calls" || fail "default dirs"
[[ -f "$default/site/index.html" && -f "$default/index.md" ]] || fail "no site or index"
grep -q "gh" "$calls" && fail "gh used without --comment"
[[ -z "$(git -C "$repo" status --porcelain)" ]] || fail "output in git: $(git -C "$repo" status --porcelain)"

# Each run clears the directory first, so output never accumulates.
touch "$default/stale.png" "$default/site/stale.html"
SHOTS_EXIT=2 expect_exit 2 --no-build /x
[[ -e "$default/stale.png" || -e "$default/site/stale.html" ]] && fail "earlier output kept"

# Pages pass through; --no-build skips pnpm; --out is honoured.
expect_exit 0 --no-build --out "$root/shots" /blog /factory
grep -q "pnpm" "$calls" && fail "--no-build still built"
grep -q -- "--out $root/shots /blog /factory\$" "$calls" || fail "pages not passed"

# Directories it must not clear or write.
mkdir -p "$root/mine" && touch "$root/mine/keep"
expect_exit 2 --no-build --out "$root/mine"
[[ -e "$root/mine/keep" ]] || fail "cleared a directory it did not make"
expect_exit 2 --no-build --out "$repo/shots"
expect_exit 2 --no-build --out /
expect_exit 0 --no-build --out "$repo/ignored/shots"
[[ -z "$(git -C "$repo" status --porcelain)" ]] || fail "ignored output shows in git"

# A failed page exits 1.
SHOTS_EXIT=1 expect_exit 1 --no-build
grep -q "screenshots in" "$root/out" || fail "failed run hides the output dir"

# A signal stops the screenshot step: no process is left behind.
for signal in TERM INT; do
	: >"$calls"
	rm -f "$calls.pid"
	(cd "$repo" && SHOTS_HANG=1 exec env --default-signal=INT bash "$script" --no-build) \
		>"$root/out" 2>&1 &
	runner=$!
	for _ in $(seq 1 100); do
		[[ -s "$calls.pid" ]] && break
		sleep 0.1
	done
	[[ -s "$calls.pid" ]] || fail "the screenshot step did not start"
	kill -"$signal" "$runner"
	got=0
	wait "$runner" || got=$?
	want=$([[ $signal == INT ]] && echo 130 || echo 143)
	[[ "$got" == "$want" ]] || fail "SIG$signal: exit $got, not $want"
	pid="$(cat "$calls.pid")"
	for _ in $(seq 1 20); do
		kill -0 "$pid" 2>/dev/null || break
		sleep 0.1
	done
	kill -0 "$pid" 2>/dev/null && kill -9 "$pid" && fail "SIG$signal left the screenshot step running"
done

# --comment with no earlier comment: one comment with marker, SHA and pages.
expect_exit 0 --no-build --comment
grep -q "api -X POST repos/owner/repo/issues/42/comments" "$calls" || fail "no comment created"
grep -q "^<!-- jomcgi-site-preview -->" "$calls.body" || fail "no marker"
grep -q "$sha" "$calls.body" || fail "no SHA"
# shellcheck disable=SC2016 # Markdown backticks, not a command.
grep -q '| `/` | ok |' "$calls.body" || fail "no page table"
grep -q "Screenshots" "$calls.body" && fail "screenshot table in the comment"

# Its own earlier comment is edited, not duplicated. A failed page still
# writes the comment, then exits 1.
EXISTING=9 SHOTS_EXIT=1 expect_exit 1 --no-build --comment --pr 42
grep -q "api -X PATCH repos/owner/repo/issues/comments/9" "$calls" || fail "own comment not edited"
grep -q "POST" "$calls" && fail "a second comment was created"
# shellcheck disable=SC2016 # Markdown backticks, not a command.
grep -q '| `/` | FAIL |' "$calls.body" || fail "failure not in the comment"

# A stale HEAD or a dirty checkout is refused before the build.
PR_HEAD="$(printf 'c%.0s' {1..40})" expect_exit 1 --comment
grep -q "push first" "$root/out" || fail "no push hint"
grep -q "pnpm" "$calls" && fail "built before refusing"
echo y >"$repo/tracked"
expect_exit 1 --comment
grep -q "clean checkout" "$root/out" || fail "no clean-checkout hint"

echo PASS
