#!/usr/bin/env bash
set -uo pipefail

SCRIPT_REL="bazel/tools/embervm/measure-vm-overhead.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/measure-vm-overhead.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
if [[ -z "$SCRIPT" ]]; then
	echo "ERROR: cannot locate measure-vm-overhead.sh in runfiles" >&2
	exit 1
fi

TEST_ROOT="${TEST_TMPDIR:-$(mktemp -d)}"
MOCK_BIN="$TEST_ROOT/bin"
mkdir -p "$MOCK_BIN"

cat >"$MOCK_BIN/kubectl" <<'EOF'
#!/usr/bin/env bash
set -u
all_args=" $* "
if [[ "$all_args" == *" get pods "* ]]; then
	if [[ "${MOCK_GET_FAIL:-0}" == "1" ]]; then
		echo "mock access denied" >&2
		exit 1
	fi
	if [[ "$all_args" != *" --selector app.kubernetes.io/component=noded-brick "* ]]; then
		echo "missing brick component selector" >&2
		exit 1
	fi
	if [[ "$all_args" != *" --field-selector status.phase=Running "* ]]; then
		echo "missing Running phase selector" >&2
		exit 1
	fi
	printf '%s\n' \
		'embervm-embervm-noded-brick-99gi-live-abc|2gi|' \
		'embervm-embervm-noded-brick-16gi-idle-def|16gi|' \
		'embervm-embervm-noded-brick-4gi-refuse-ghi||' \
		'embervm-embervm-noded-brick-8gi-terminating-jkl|8gi|2026-09-05T12:00:00Z'
	exit 0
fi
if [[ "$all_args" == *" exec embervm-embervm-noded-brick-4gi-refuse-ghi "* ]]; then
	echo "mock exec forbidden" >&2
	exit 1
fi
if [[ "$all_args" == *"/proc/1/task/"*"/children"* ]]; then
	if [[ "$all_args" == *"99gi-live-abc"* ]]; then
		printf '101\tfirecracker\n'
	fi
	exit 0
fi
if [[ "$all_args" == *" cat /proc/101/smaps "* ]]; then
	case "${MOCK_LAYOUT:-anonymous}" in
	direct)
		guest_path='/var/lib/embervm/scratch/embervm-noded/snapshots/bases/wl__abc/memfile'
		;;
	jailed)
		guest_path='/var/lib/embervm/scratch/embervm-noded/i/thread/jailer/firecracker/vm-1/root/snapshot/memfile'
		;;
	*)
		guest_path=''
		;;
	esac
	cat <<SMAPS
00400000-00420000 r-xp 00000000 08:01 10 /opt/fc/firecracker
Size:                128 kB
Rss:                3000 kB
Pss:                1000 kB
7f000000-80000000 rw-p 00000000 00:00 0 $guest_path
Size:             262144 kB
Rss:              250000 kB
Pss:              249000 kB
80000000-80001000 rw-p 00000000 00:00 0 [heap]
Size:               4096 kB
Rss:                1000 kB
Pss:                1000 kB
SMAPS
	exit 0
fi
if [[ "$all_args" == *" cat /var/lib/embervm/scratch/embervm-noded/snapshots/bases/wl__abc/mem_mib "* ]]; then
	printf '256\n'
	exit 0
fi
echo "unexpected kubectl invocation: $*" >&2
exit 1
EOF
chmod +x "$MOCK_BIN/kubectl"

cat >"$MOCK_BIN/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$MOCK_BIN/sleep"

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

assert_contains() {
	local file="$1" pattern="$2" label="$3"
	grep -qE "$pattern" "$file" || fail "$label"
}

assert_not_contains() {
	local file="$1" pattern="$2" label="$3"
	if grep -qE "$pattern" "$file"; then
		fail "$label"
	fi
}

bash "$SCRIPT" --self-test >"$TEST_ROOT/self-test.out" 2>&1 ||
	fail "inline smaps self-test failed"
assert_contains "$TEST_ROOT/self-test.out" 'self-test passed' "self-test did not report success"

PATH="$MOCK_BIN:$PATH" bash "$SCRIPT" --context test --namespace test \
	--interval 1 --count 2 >"$TEST_ROOT/out.jsonl" 2>"$TEST_ROOT/err"
rc=$?
[[ "$rc" == "0" ]] || fail "sampling exited $rc"

[[ "$(grep -c '"pid":101' "$TEST_ROOT/out.jsonl")" == "2" ]] ||
	fail "expected two guest sample lines"
[[ "$(grep -c '"guests":0' "$TEST_ROOT/out.jsonl")" == "2" ]] ||
	fail "expected two idle-brick lines"
assert_contains "$TEST_ROOT/out.jsonl" '"declared_mib_or_null":null' "declared memory was not null"
assert_contains "$TEST_ROOT/out.jsonl" '"declared_memory_source":"unavailable"' "unavailable source missing"
assert_contains "$TEST_ROOT/out.jsonl" '"rss_total_mib":248\.047' "total RSS arithmetic is wrong"
assert_contains "$TEST_ROOT/out.jsonl" '"pss_total_mib":245\.117' "total PSS arithmetic is wrong"
assert_contains "$TEST_ROOT/out.jsonl" '"guest_mapping_rss_mib":244\.141' "guest mapping RSS is wrong"
assert_contains "$TEST_ROOT/out.jsonl" '"guest_mapping_pss_mib":243\.164' "guest mapping PSS is wrong"
assert_contains "$TEST_ROOT/out.jsonl" '"guest_mapping_size_mib":256\.000' "guest mapping VMA size is wrong"
assert_contains "$TEST_ROOT/out.jsonl" '"overhead_rss_mib":3\.906' "RSS overhead arithmetic is wrong"
assert_contains "$TEST_ROOT/out.jsonl" '"overhead_pss_mib":1\.953' "PSS overhead arithmetic is wrong"
assert_contains "$TEST_ROOT/out.jsonl" '"type":"summary","class":"2gi","sample_count":2' "summary missing"
assert_contains "$TEST_ROOT/out.jsonl" '"suggested_noded.vmOverheadMib":16' "suggestion rounding is wrong"
assert_contains "$TEST_ROOT/out.jsonl" '"type":"summary","class":"16gi","sample_count":0' "idle summary missing"
assert_contains "$TEST_ROOT/out.jsonl" '"median_overhead_pss_mib":1\.953' "summary did not use PSS overhead"
assert_contains "$TEST_ROOT/out.jsonl" '"median_overhead_pss_mib":null' "empty summary statistics are not null"
assert_contains "$TEST_ROOT/err" 'mock exec forbidden' "exec refusal was not reported"
assert_not_contains "$TEST_ROOT/out.jsonl" 'terminating' "terminating brick was sampled"
assert_not_contains "$TEST_ROOT/err" 'terminating' "terminating brick emitted a warning"

MOCK_LAYOUT=direct PATH="$MOCK_BIN:$PATH" bash "$SCRIPT" --once \
	>"$TEST_ROOT/memfile.jsonl" 2>"$TEST_ROOT/memfile.err"
rc=$?
[[ "$rc" == "0" ]] || fail "memfile sampling exited $rc"
assert_contains "$TEST_ROOT/memfile.jsonl" '"declared_mib_or_null":256' "mem_mib sidecar was not read"
assert_contains "$TEST_ROOT/memfile.jsonl" '"declared_memory_source":"mem_mib_sidecar"' "sidecar source missing"

MOCK_LAYOUT=jailed PATH="$MOCK_BIN:$PATH" bash "$SCRIPT" --once \
	>"$TEST_ROOT/jailed.jsonl" 2>"$TEST_ROOT/jailed.err"
rc=$?
[[ "$rc" == "0" ]] || fail "jailed sampling exited $rc"
assert_contains "$TEST_ROOT/jailed.jsonl" '"declared_mib_or_null":null' "jailed declared memory was not null"
assert_contains "$TEST_ROOT/jailed.jsonl" '"declared_memory_source":"jailed_layout_unsupported"' "jailed source missing"

set +e
MOCK_GET_FAIL=1 PATH="$MOCK_BIN:$PATH" bash "$SCRIPT" --once \
	>"$TEST_ROOT/get-fail.out" 2>"$TEST_ROOT/get-fail.err"
rc=$?
set -e
[[ "$rc" != "0" ]] || fail "kubectl get failure returned success"
assert_contains "$TEST_ROOT/get-fail.err" 'mock access denied' "kubectl get error was not printed"

echo "All measure-vm-overhead tests passed"
