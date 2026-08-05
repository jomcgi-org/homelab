#!/bin/sh
set -eu

binary="$1"
home="$TEST_TMPDIR/home"
mkdir -p "$home"

version="$($binary --version)"
case "$version" in
*"2.1.220"*) ;;
*)
	echo "unexpected Claude version: $version" >&2
	exit 1
	;;
esac

session_id=00000000-0000-0000-0000-000000000000
input=$(printf '%s\n' "{\"type\":\"user\",\"session_id\":\"$session_id\",\"message\":{\"role\":\"user\",\"content\":\"bazel smoke test\"}}")
output=$(printf '%s\n' "$input" | HOME="$home" timeout 30 "$binary" -p \
	--input-format stream-json --output-format stream-json --verbose --max-turns 1 2>&1 || true)

case "$output" in
*"No conversation found with session ID: $session_id"*) ;;
*)
	echo "late-bound resume lookup did not run:\n$output" >&2
	exit 1
	;;
esac

case "$output" in
*'"total_cost_usd":0'*) ;;
*)
	echo "smoke test unexpectedly made a model turn:\n$output" >&2
	exit 1
	;;
esac

fresh_output=$(printf '%s\n' '{"type":"user","message":{"role":"user","content":"fresh create smoke test"}}' |
	HOME="$home" timeout 30 "$binary" -p --input-format stream-json \
		--output-format stream-json --verbose --max-turns 0 2>&1 || true)

case "$fresh_output" in
*'"type":"result"'*) ;;
*)
	echo "session-less first message was not re-injected:\n$fresh_output" >&2
	exit 1
	;;
esac
