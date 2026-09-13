#!/bin/sh
set -eu

engine=$1
input=$2
output=$("$engine" 'call($X, ...)' "$input")

case "$output" in
  *needle*) ;;
  *)
    echo "spacegrep did not report the expected metavariable match" >&2
    echo "$output" >&2
    exit 1
    ;;
esac
