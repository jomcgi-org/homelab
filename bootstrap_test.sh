#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${TEST_SRCDIR}/${TEST_WORKSPACE}"
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

mkdir -p "$TEMP_ROOT/bin" "$TEMP_ROOT/image/usr/bin"
for command in agent-run hf2oci bb claude buildifier shellcheck eslint; do
	printf '#!/bin/sh\nexit 0\n' >"$TEMP_ROOT/image/usr/bin/$command"
	chmod 0755 "$TEMP_ROOT/image/usr/bin/$command"
done
tar -C "$TEMP_ROOT/image" -cf "$TEMP_ROOT/image.tar" .

cat >"$TEMP_ROOT/bin/crane" <<'CRANE'
#!/bin/sh
case "$1" in
digest)
	printf '%s\n' 'sha256:bootstrap-test'
	;;
export)
	cat "$BOOTSTRAP_TEST_IMAGE"
	;;
*)
	exit 1
	;;
esac
CRANE
chmod 0755 "$TEMP_ROOT/bin/crane"

BOOTSTRAP_TEST_IMAGE="$TEMP_ROOT/image.tar" \
	XDG_CACHE_HOME="$TEMP_ROOT/cache" \
	PATH="$TEMP_ROOT/bin:/usr/bin:/bin" \
	bash "$REPO_ROOT/bootstrap.sh"

TOOLS_ROOT="$TEMP_ROOT/cache/homelab-tools"
for command in agent-run hf2oci bb claude buildifier shellcheck eslint; do
	test -x "$TOOLS_ROOT/usr/bin/$command"
	"$TOOLS_ROOT/usr/bin/$command"
done
test "$(cat "$TOOLS_ROOT/.digest")" = "sha256:bootstrap-test"

# A matching digest must not accept an incomplete cache. Bootstrap repairs it.
rm "$TOOLS_ROOT/usr/bin/eslint"
BOOTSTRAP_TEST_IMAGE="$TEMP_ROOT/image.tar" \
	XDG_CACHE_HOME="$TEMP_ROOT/cache" \
	PATH="$TEMP_ROOT/bin:/usr/bin:/bin" \
	bash "$REPO_ROOT/bootstrap.sh"
test -x "$TOOLS_ROOT/usr/bin/eslint"
