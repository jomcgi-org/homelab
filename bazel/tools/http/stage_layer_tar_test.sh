#!/usr/bin/env bash
# Unit tests for stage_layer_tar.sh.
#
# The script archives a staged tree into an image layer, so its ONE job is to
# be byte-identical on every machine. The assertions below are the three fields
# that are actually machine-derived (order, owner, mtime) plus the empty-input
# guard. The order assertion is the load-bearing one: #4598 sorted the member
# list but let tar recurse into the directories in that list, which silently
# re-derived readdir order and shipped 13113 entries for 1601 unique paths.
set -o errexit -o nounset -o pipefail

SCRIPT_REL="bazel/tools/http/stage_layer_tar.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/stage_layer_tar.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
if [[ -z "$SCRIPT" ]]; then
	echo "ERROR: cannot locate stage_layer_tar.sh in runfiles" >&2
	exit 1
fi
# Absolute, because one case below runs the script from a different cwd.
SCRIPT="$(cd "${SCRIPT%/*}" && pwd)/${SCRIPT##*/}"

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAILURES=0

fail() {
	echo "FAIL: $*" >&2
	FAILURES=$((FAILURES + 1))
}

pass() {
	echo "ok: $*"
}

# Stage the same logical tree twice, creating the entries in opposite orders so
# the two directories differ in readdir order on filesystems that return
# creation order. Content is identical, so the layers must be too.
stage_tree() {
	local root="$1"
	shift
	mkdir -p "$root/opt/distdir" "$root/opt/abseil/absl"
	for name in "$@"; do
		echo "content of $name" >"$root/opt/distdir/$name"
		echo "source of $name" >"$root/opt/abseil/absl/$name.cc"
	done
	# One executable file, to catch a future mode normalisation that would
	# strip the bit off the scripts in the staged tree.
	printf '#!/bin/sh\necho hi\n' >"$root/opt/abseil/run.sh"
	chmod 755 "$root/opt/abseil/run.sh"
}

FORWARD="$TMP/forward"
REVERSE="$TMP/reverse"
stage_tree "$FORWARD" zeta mid alpha rules_cc platforms
stage_tree "$REVERSE" platforms rules_cc alpha mid zeta

sh "$SCRIPT" "$FORWARD" "$TMP/forward.tar"
sh "$SCRIPT" "$REVERSE" "$TMP/reverse.tar"

# --- 1. byte-identical across creation orders -------------------------------
if cmp -s "$TMP/forward.tar" "$TMP/reverse.tar"; then
	pass "same content staged in a different order produces an identical tar"
else
	fail "tars differ between two identical trees staged in different orders"
fi

# --- 2. members follow the sorted list, exactly once each -------------------
# Compared against the sorted find list rather than checked for sortedness on
# its own: this catches duplicates and dropped entries at the same time. tar
# prints directories with a trailing slash, which find does not, so strip it.
(cd "$FORWARD" && find . -print | LC_ALL=C sort) >"$TMP/expected"
tar -tf "$TMP/forward.tar" | sed 's:/$::' >"$TMP/actual"
if diff -u "$TMP/expected" "$TMP/actual" >"$TMP/order.diff"; then
	pass "archived members match the sorted member list with no duplicates"
else
	fail "archived members are not the sorted list (readdir order or duplicates):"
	head -20 "$TMP/order.diff" >&2
fi

# --- 3. owner is pinned, not the building user ------------------------------
# GNU tar prints the pair as "0/0"; bsdtar prints uid and gid as separate
# columns. Accept either, and print the offending line if neither holds.
#
# This case can only distinguish pinned from unpinned when the test runs as a
# non-root uid, since an unpinned tar run as root also records 0. That holds
# where it matters: the layer this guards recorded uid 1001 when it drifted, so
# the CI executor is not root.
OWNER_LINE=$(tar -tvf "$TMP/forward.tar" | head -1)
if grep -qE '(^|[[:space:]])0/0([[:space:]]|$)' <<<"$OWNER_LINE" ||
	awk '{exit !($3 == 0 && $4 == 0)}' <<<"$OWNER_LINE"; then
	pass "owner and group are recorded as 0"
else
	fail "owner is not normalised, tar records: $OWNER_LINE"
fi

# --- 4. mtimes are pinned to the epoch --------------------------------------
# Read the stored mtime numerically so the check does not depend on the local
# timezone (bsdtar renders epoch 0 as 31 Dec 1969 west of UTC).
EXTRACT="$TMP/extract"
mkdir -p "$EXTRACT"
tar -xf "$TMP/forward.tar" -C "$EXTRACT"
MTIME=$(TZ=UTC date -r "$EXTRACT/opt/distdir/alpha" -u '+%Y' 2>/dev/null ||
	stat -c '%Y' "$EXTRACT/opt/distdir/alpha")
if [[ "$MTIME" == "1970" || "$MTIME" == "0" ]]; then
	pass "mtimes are pinned to the epoch"
else
	fail "mtime is not pinned, got: $MTIME"
fi

# --- 4b. modes survive ------------------------------------------------------
# The staged tree carries an executable script, and the script under test
# deliberately does NOT normalise modes. Without this assertion the executable
# staged above is decorative: a future blanket chmod would strip the bit and
# every other case would still pass.
if [[ -x "$EXTRACT/opt/abseil/run.sh" ]]; then
	pass "the executable bit survives archiving"
else
	fail "the executable bit was stripped from opt/abseil/run.sh"
fi

# --- 4c. the epoch is UTC, not the builder's local midnight -----------------
# `touch -t` reads its argument as LOCAL time, so without a TZ pin the same
# tree staged in two timezones stores different mtimes and therefore different
# bytes. Uses a POSIX offset string rather than a zone name so it does not
# depend on tzdata being present.
TZSHIFT="$TMP/tzshift"
stage_tree "$TZSHIFT" zeta mid alpha rules_cc platforms
TZ='XXX-10' sh "$SCRIPT" "$TZSHIFT" "$TMP/tzshift.tar"
if cmp -s "$TMP/tzshift.tar" "$TMP/forward.tar"; then
	pass "the archive is identical regardless of the builder's timezone"
else
	fail "the builder's timezone changes the archive bytes"
fi

# --- 5. an empty staged root is refused, not silently archived --------------
mkdir -p "$TMP/empty"
if sh "$SCRIPT" "$TMP/empty" "$TMP/empty.tar" 2>/dev/null; then
	fail "an empty staged root produced a layer instead of failing"
else
	pass "an empty staged root is refused"
fi

# --- 6. a relative output path resolves against the caller's cwd ------------
(cd "$TMP" && sh "$SCRIPT" "$FORWARD" "relative.tar")
if [[ -f "$TMP/relative.tar" ]] && cmp -s "$TMP/relative.tar" "$TMP/forward.tar"; then
	pass "a relative output path is written relative to the caller"
else
	fail "a relative output path did not land next to the caller's cwd"
fi

if ((FAILURES > 0)); then
	echo "$FAILURES stage-layer-tar test(s) failed" >&2
	exit 1
fi
echo "All stage-layer-tar tests passed"
