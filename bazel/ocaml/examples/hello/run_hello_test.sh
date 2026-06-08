#!/bin/sh
# Runs the linked native binary and asserts it prints the expected greeting.
set -eu

bin="$1"
out="$("$bin")"
echo "$out"
echo "$out" | grep -q "greetings, Bazel!" || {
	echo "FAIL: expected greeting not found in output" >&2
	exit 1
}
