#!/usr/bin/env bash
# Read-only host checks for the single-host EmberVM profile.
set -o errexit -o nounset -o pipefail

minimum_memory_mib=${EMBER_QUICKSTART_MIN_MEMORY_MIB:-8192}
minimum_disk_mib=${EMBER_QUICKSTART_MIN_DISK_MIB:-30720}
failures=0

pass() {
	printf 'PASS: %s\n' "$1"
}

fail() {
	printf 'FAIL: %s\n' "$1" >&2
	failures=$((failures + 1))
}

if [[ $(uname -s) == Linux ]]; then
	pass "Linux host"
else
	fail "Linux is required"
fi

if [[ $(uname -m) == x86_64 ]]; then
	pass "x86_64 architecture"
else
	fail "x86_64 is required by the published control, noded, and runtime-python images"
fi

if [[ -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]; then
	pass "/dev/kvm is a readable and writable character device"
else
	fail "/dev/kvm must exist and be readable and writable by the k3s runtime"
fi

if grep -Eq '(^|[[:space:]])(vmx|svm)([[:space:]]|$)' /proc/cpuinfo; then
	pass "hardware virtualization flag is visible"
else
	fail "neither vmx nor svm is visible in /proc/cpuinfo"
fi

memory_mib=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)
if ((memory_mib >= minimum_memory_mib)); then
	pass "memory ${memory_mib} MiB is at least ${minimum_memory_mib} MiB"
else
	fail "memory ${memory_mib} MiB is below ${minimum_memory_mib} MiB"
fi

disk_mib=$(df -Pm /var/lib | awk 'NR == 2 {print $4}')
if ((disk_mib >= minimum_disk_mib)); then
	pass "free space under /var/lib is ${disk_mib} MiB"
else
	fail "free space under /var/lib is ${disk_mib} MiB, below ${minimum_disk_mib} MiB"
fi

for tool in helm kubectl python3 curl jq sha256sum od; do
	if command -v "$tool" >/dev/null 2>&1; then
		pass "$tool is installed"
	else
		fail "$tool is required"
	fi
done

if ((failures > 0)); then
	printf '%d prerequisite check(s) failed\n' "$failures" >&2
	exit 1
fi
