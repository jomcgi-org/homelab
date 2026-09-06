#!/usr/bin/env bash
# Shared execution boundary. Status 1 means findings; status 2 means infrastructure.

set -E
trap 'echo "INFRASTRUCTURE: wrapper command failed at line $LINENO" >&2; exit 2' ERR

SEMGREP_OUTPUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/semgrep-output.py"

semgrep_error() {
	echo "INFRASTRUCTURE: $*" >&2
	exit 2
}

semgrep_setup() {
	local search_root core pro dependency probe version status
	search_root="${RUNFILES_DIR:-.}"
	core=$(find "$search_root" -name semgrep-core \( -type f -o -type l \) -print -quit)
	pro=$(find "$search_root" -name semgrep-core-proprietary \( -type f -o -type l \) -print -quit)
	[[ -n "$core" && -n "$pro" ]] || semgrep_error "both OSS and Pro engines are required in runfiles"

	# Stage dependencies before probing: both engines use $ORIGIN/libs, and
	# Pro also needs the OSS executable beside it.
	PRO_DIR="${TEST_TMPDIR}/pro_bin"
	mkdir -p "$PRO_DIR/libs"
	cp "$core" "$PRO_DIR/semgrep-core"
	cp "$pro" "$PRO_DIR/semgrep-core-proprietary"
	chmod 755 "$PRO_DIR/semgrep-core" "$PRO_DIR/semgrep-core-proprietary"
	for dependency in "$core" "$pro"; do
		if [[ -d "$(dirname "$dependency")/libs" ]]; then
			cp -RL "$(dirname "$dependency")/libs/." "$PRO_DIR/libs/"
		fi
	done
	ENGINE="$PRO_DIR/semgrep-core-proprietary"
	export SEMGREP_APP_TOKEN="${SEMGREP_APP_TOKEN:-offline}"
	export SEMGREP_URL="${SEMGREP_URL:-http://127.0.0.1:0}"
	for probe in semgrep-core semgrep-core-proprietary; do
		status=0
		version=$("$PRO_DIR/$probe" -version 2>"${TEST_TMPDIR}/probe.stderr") || status=$?
		if [[ "$status" -ne 0 || -z "${version//[[:space:]]/}" ]]; then
			head -c 4096 "${TEST_TMPDIR}/probe.stderr" >&2
			semgrep_error "$probe version probe failed (exit=$status)"
		fi
		echo "ENGINE: $probe version=${version:0:160}"
		if [[ "$probe" == semgrep-core ]]; then
			SEMGREP_ENGINE_VERSION="$version"
			export SEMGREP_ENGINE_VERSION
		fi
	done
	if [[ "${SEMGREP_TEST_MODE:-}" == 1 ]]; then
		semgrep_error "annotation comparison is not implemented; SEMGREP_TEST_MODE=1 cannot pass without checking expected and unexpected findings (#4777)"
	fi
}

semgrep_run_pass() {
	local label="${1:0:400}" status=0 started=$SECONDS
	shift
	echo "PASS START: index=$RESULT_INDEX $label"
	"$ENGINE" "$@" >"$RESULT_FILE" 2>"$STDERR_FILE" || status=$?
	if [[ "$status" -ne 0 ]]; then
		echo "PASS END: index=$RESULT_INDEX exit=$status elapsed=$((SECONDS - started))s"
		head -c 4096 "$STDERR_FILE" >&2
		semgrep_error "$label engine exit=$status"
	fi
	if ! python3 "$SEMGREP_OUTPUT" validate "$RESULT_FILE"; then
		echo "PASS END: index=$RESULT_INDEX exit=0 output=invalid elapsed=$((SECONDS - started))s"
		head -c 4096 "$STDERR_FILE" >&2
		semgrep_error "$label returned invalid or incomplete core output"
	fi
	echo "PASS END: index=$RESULT_INDEX exit=0 elapsed=$((SECONDS - started))s"
}

semgrep_merge() {
	SCAN_EXIT=0
	python3 "$SEMGREP_OUTPUT" merge "$RESULTS_DIR" "$MERGED_FILE" "$RESULT_INDEX" "$SCAN_DIR" || SCAN_EXIT=$?
	[[ "$SCAN_EXIT" -le 1 ]] || exit 2
}

semgrep_finish() {
	# Infrastructure has already failed before either finding filter runs.
	local status=0
	python3 "$SEMGREP_OUTPUT" finish "$MERGED_FILE" ${EXCLUDE_IDS[@]+"${EXCLUDE_IDS[@]}"} || status=$?
	exit "$status"
}
