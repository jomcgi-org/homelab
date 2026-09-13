#!/bin/sh
set -eu

binary="$1"
case "$binary" in
/*) ;;
*) binary="$PWD/$binary" ;;
esac

segments="$(mktemp)"
output="$(mktemp)"
trap 'rm -f "$segments" "$output"' EXIT

readelf -l "$binary" >"$segments"
if grep -q 'INTERP' "$segments"; then
	echo "toycaml static link test: ELF interpreter found" >&2
	exit 1
fi

"$binary" 'foo($X, 2)' 'foo(bar(7), 2)' >"$output"
grep -q '^match!$' "$output"
"$binary" --help=plain >"$output"
grep -q 'PATTERN' "$output"
echo "toycaml static link: no PT_INTERP, non-dune Cmdliner CLI passed"
