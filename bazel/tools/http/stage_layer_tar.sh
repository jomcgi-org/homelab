#!/usr/bin/env sh
# stage_layer_tar.sh: archive a staged tree into a byte-reproducible tar layer.
#
# Usage: stage_layer_tar.sh <staged_root> <output_tar>
#
# The archive MUST be identical on every machine that builds it: it becomes an
# image layer, so any drift changes the image digest and makes the chart look
# like it pins a rebuilt image (issue #4594).
#
# Three environment-derived fields have to be pinned, and the third is the one
# that is easy to get wrong:
#
#   mtime   `touch -h -t` pins every entry to the epoch, so extraction time
#           does not leak in.
#   owner   `--owner=0 --group=0` (with `--numeric-owner`, so no name lookup
#           happens either). `--numeric-owner` ALONE is not enough: it only
#           suppresses the uid to name lookup and still records the building
#           user's numeric uid, which is how the published layer ended up
#           recording uid/gid 1001.
#   order   a sorted member list AND `--no-recursion`. Sorting the list on its
#           own does nothing, because tar treats every DIRECTORY in a `-T` list
#           as "archive this subtree" and walks it in readdir order. Feeding it
#           a list that starts with `.` therefore re-derives the whole tree in
#           the machine's readdir order and appends the sorted files after it.
#           That is what #4598 shipped: the published layer held 13113 entries
#           for 1601 unique paths, still in readdir order, so the digest kept
#           drifting per machine. `--no-recursion` makes the list authoritative.
#
# File modes are deliberately NOT normalised. Nothing observed has drifted
# there, and a blanket chmod would have to preserve the executable bit on the
# scripts in the staged tree, which `a=,u+rwX` does not do (the `a=` clears the
# bit before `X` tests for it).
#
# Portability: every flag used here is accepted by both GNU tar and bsdtar, so
# the repo rule that calls this still fetches on a Mac.
set -eu

ROOT="${1:?staged root dir required}"
OUT="${2:?output tar path required}"

# Resolve the output before cd, so a relative path stays relative to the caller.
case "$OUT" in
/*) ;;
*) OUT="$(pwd)/$OUT" ;;
esac

cd "$ROOT"

# Pin every mtime, symlinks included, to the epoch.
find . -exec touch -h -t 197001010000 {} +

# Materialise the sorted member list instead of piping it. /bin/sh on the CI
# runner is dash, which has no `pipefail`, so a failing `find` in a pipeline
# would silently produce a short (or empty) tar that still exits 0.
LIST="${TMPDIR:-/tmp}/stage_layer_tar.$$.list"
trap 'rm -f "$LIST"' EXIT
find . -print | LC_ALL=C sort >"$LIST"
# `find .` always emits "." itself, so a one-line list means either the staged
# root is empty or find failed. Either way the layer would ship nothing, which
# is a silent failure once it is inside an image.
if [ "$(wc -l <"$LIST")" -lt 2 ]; then
	echo "stage_layer_tar: staged root '$ROOT' holds no entries, refusing to write an empty layer" >&2
	exit 1
fi

tar --no-recursion --numeric-owner --owner=0 --group=0 -cf "$OUT" -T "$LIST"
