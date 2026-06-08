#!/bin/sh
# Runs the linked native binary and asserts it prints the expected greeting.
# Executes inside the pinned OCaml container (same glibc the binary linked
# against), set via exec_properties on the sh_test target.
set -eu

bin="$1"
out="$("$bin")"
echo "$out"
echo "$out" | grep -q "greetings, Bazel!" || {
	echo "FAIL: expected greeting not found in output" >&2
	exit 1
}
