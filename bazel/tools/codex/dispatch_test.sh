#!/usr/bin/env bash
set -euo pipefail

SCRIPT="${BASH_SOURCE[0]%/*}/dispatch.sh"
[[ -f "$SCRIPT" ]] || exit 1
TMP="${TEST_TMPDIR:-$(mktemp -d)}"
BIN="$TMP/bin"
mkdir -p "$BIN"
cat >"$BIN/codex" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
last=""
while (($#)); do
  if [[ "$1" == --output-last-message ]]; then last="$2"; shift 2; else shift; fi
done
printf 'run\n' >>"${STUB_COUNTER:?}"
sleep "${STUB_SLEEP:-0.1}"
[[ -z "${STUB_OUTPUT:-}" ]] || printf '%s\n' "$STUB_OUTPUT"
if [[ "${STUB_WRITE_LAST:-1}" == 1 ]]; then
  mkdir -p "${last%/*}"
  printf '%s\n' "${STUB_MESSAGE:-stub final message}" >"$last"
fi
exit "${STUB_EXIT:-0}"
EOF
chmod +x "$BIN/codex"
cat >"$BIN/opencode" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'run\n' >>"${STUB_COUNTER:?}"
printf '%s\n' "$@" >"${STUB_OPENCODE_ARGS:?}"
sleep "${STUB_SLEEP:-0.1}"
[[ -z "${STUB_OUTPUT:-}" ]] || printf '%s\n' "$STUB_OUTPUT"
exit "${STUB_EXIT:-0}"
EOF
chmod +x "$BIN/opencode"

run_dispatch() {
	local workdir="$1"
	local tier="${2:-luna}"
	PATH="$BIN:$PATH" STUB_COUNTER="$TMP/counter" \
		STUB_SLEEP="${STUB_SLEEP:-0.1}" STUB_EXIT="${STUB_EXIT:-0}" \
		STUB_MESSAGE="${STUB_MESSAGE:-stub final message}" STUB_WRITE_LAST="${STUB_WRITE_LAST:-1}" \
		STUB_OUTPUT="${STUB_OUTPUT:-}" STUB_OPENCODE_ARGS="$TMP/opencode-args" \
		bash "$SCRIPT" "$tier" "$workdir" test-spec
}
assert_eq() {
	[[ "$2" == "$3" ]] || {
		echo "FAIL: $1" >&2
		exit 1
	}
	echo "PASS: $1"
}
wait_for_file() {
	for _ in {1..100}; do
		[[ -s "$1" ]] && return
		sleep 0.01
	done
	echo "FAIL: timeout" >&2
	exit 1
}
reset() {
	: >"$TMP/counter"
	: >"$TMP/opencode-args"
	STUB_SLEEP=0.1 STUB_EXIT=0 STUB_MESSAGE="stub final message" STUB_WRITE_LAST=1 STUB_OUTPUT=
}

set +e
usage_out=$(bash "$SCRIPT" invalid "$TMP" test-spec 2>&1)
usage_rc=$?
set -e
assert_eq "usage error exit" 64 "$usage_rc"
grep -q '<ox|luna|terra|frontier>' <<<"$usage_out"
echo "PASS: usage lists ox"

NO_OPENCODE="$TMP/no-opencode"
mkdir -p "$NO_OPENCODE"
set +e
missing_out=$(PATH="$NO_OPENCODE" /bin/bash "$SCRIPT" ox "$TMP" test-spec 2>&1)
missing_rc=$?
set -e
assert_eq "missing opencode exit" 64 "$missing_rc"
grep -q 'opencode not on PATH; use tier frontier' <<<"$missing_out"
echo "PASS: missing opencode fallback"

reset
WORK="$TMP/happy"
mkdir -p "$WORK"
out=$(run_dispatch "$WORK")
assert_eq "happy exit" 0 "$?"
grep -q "stub final message" <<<"$out"
log=$(sed -n 's/.*transcript: \([^)]*\).*/\1/p' <<<"$out")
[[ -f "$log" ]] || exit 1
echo "PASS: happy output and transcript"

reset
WORK="$TMP/ox"
mkdir -p "$WORK"
out=$(STUB_OUTPUT="opencode final message" run_dispatch "$WORK" ox)
assert_eq "ox exit" 0 "$?"
grep -q "opencode final message" <<<"$out"
assert_eq "ox command" run "$(sed -n '1p' "$TMP/opencode-args")"
assert_eq "ox workdir flag" --dir "$(sed -n '2p' "$TMP/opencode-args")"
assert_eq "ox workdir" "$WORK" "$(sed -n '3p' "$TMP/opencode-args")"
assert_eq "ox model flag" -m "$(sed -n '4p' "$TMP/opencode-args")"
assert_eq "ox model" openrouter/stealth/ox-alpha "$(sed -n '5p' "$TMP/opencode-args")"
assert_eq "ox variant flag" --variant "$(sed -n '6p' "$TMP/opencode-args")"
assert_eq "ox effort" high "$(sed -n '7p' "$TMP/opencode-args")"
assert_eq "ox auto flag" --auto "$(sed -n '8p' "$TMP/opencode-args")"
echo "PASS: ox output and invocation"

reset
WORK="$TMP/held"
mkdir -p "$WORK"
STUB_SLEEP=0.7 run_dispatch "$WORK" >"$TMP/holder.out" 2>&1 &
holder=$!
wait_for_file "$WORK/.codex-dispatch/lock/child"
set +e
held_out=$(run_dispatch "$WORK" 2>&1)
held_rc=$?
set -e
wait "$holder"
assert_eq "held lock exit" 65 "$held_rc"
assert_eq "held lock worker count" 1 "$(wc -l <"$TMP/counter" | tr -d ' ')"
grep -q "live log:" <<<"$held_out"
echo "PASS: held lock"

reset
WORK="$TMP/concurrent"
mkdir -p "$WORK"
pids=()
for _ in {1..8}; do
	STUB_SLEEP=0.5 run_dispatch "$WORK" >"$TMP/out.$RANDOM" 2>&1 &
	pids+=("$!")
done
rcs=()
for pid in "${pids[@]}"; do
	set +e
	wait "$pid"
	rcs+=("$?")
	set -e
done
assert_eq "concurrency worker count" 1 "$(wc -l <"$TMP/counter" | tr -d ' ')"
busy=0
for rc in "${rcs[@]}"; do
	if [[ "$rc" == 65 ]]; then busy=$((busy + 1)); else assert_eq "concurrency winner exit" 0 "$rc"; fi
done
assert_eq "concurrency busy count" 7 "$busy"
echo "PASS: concurrency"

reset
WORK="$TMP/stale"
mkdir -p "$WORK/.codex-dispatch/lock"
sleep 0.01 &
dead=$!
wait "$dead"
printf '%s\n' "$dead" >"$WORK/.codex-dispatch/lock/pid"
printf '%s\n' "$dead" >"$WORK/.codex-dispatch/lock/child"
printf '%s\n' old.log >"$WORK/.codex-dispatch/lock/log"
set +e
run_dispatch "$WORK" >/dev/null
stale_rc=$?
set -e
assert_eq "stale reclaim exit" 0 "$stale_rc"
sleep 1 &
live_child=$!
mkdir -p "$WORK/.codex-dispatch/lock"
printf '%s\n' "$dead" >"$WORK/.codex-dispatch/lock/pid"
printf '%s\n' "$live_child" >"$WORK/.codex-dispatch/lock/child"
set +e
live_out=$(run_dispatch "$WORK" 2>&1)
live_rc=$?
set -e
wait "$live_child"
assert_eq "live stale child exit" 65 "$live_rc"
grep -q "pid $live_child" <<<"$live_out"
echo "PASS: stale reclaim"

reset
WORK="$TMP/rerun"
mkdir -p "$WORK"
first_out=$(STUB_MESSAGE=first-message run_dispatch "$WORK")
old_log=$(sed -n 's/.*transcript: \([^)]*\).*/\1/p' <<<"$first_out")
set +e
second_out=$(STUB_EXIT=3 STUB_WRITE_LAST=0 run_dispatch "$WORK" 2>&1)
second_rc=$?
set -e
assert_eq "failed rerun exit" 3 "$second_rc"
! grep -q first-message <<<"$second_out"
[[ -f "$old_log" ]] || exit 1
echo "PASS: same-second rerun"

reset
WORK="$TMP/quota"
mkdir -p "$WORK"
set +e
STUB_EXIT=1 STUB_WRITE_LAST=0 STUB_OUTPUT="pytest collected 429 items" run_dispatch "$WORK" >"$TMP/plain.out" 2>&1
plain_rc=$?
set -e
assert_eq "bare 429 is not quota" 1 "$plain_rc"
set +e
STUB_EXIT=1 STUB_WRITE_LAST=0 STUB_OUTPUT="429 Too Many Requests" run_dispatch "$WORK" >"$TMP/framed.out" 2>&1
framed_rc=$?
set -e
assert_eq "framed 429 is quota" 42 "$framed_rc"
echo "PASS: quota regex"
echo "All dispatch tests passed"
