#!/usr/bin/env bash
# Removes build and test leftovers from a workstation node.
#
# On 2026-11-23 these leftovers filled node-1's root filesystem. The kubelet
# then tainted the node for disk pressure and left 22 pods Pending. The script
# lists and, with --apply, removes only these three kinds of file:
#
#   1. Go build cache files not modified for --go-days (default 3), under
#      GOCACHE. The directory must hold Go's README marker.
#   2. Test directories /tmp/pgdata* and /tmp/tmp* (direct children of
#      GC_TMP_DIR, owned by the caller, not symlinks) that are older than
#      --tmp-hours (default 12), hold no newer file, have no open file,
#      working directory or command line in any process the caller can read,
#      and are not named by a postgres process.
#   3. Bazel disk cache files (ac/ and cas/ only) not modified for
#      --bazel-days (default 7), under BAZEL_DISK_CACHE.
#
# It touches nothing else. It refuses to run as root, and it does not install
# a timer. Dry run is the default.
#
# Usage:
#   node-disk-gc.sh [--apply] [--go-days N] [--tmp-hours N] [--bazel-days N]
#
# Environment:
#   GOCACHE            Go build cache (default: ~/.cache/go-build)
#   BAZEL_DISK_CACHE   Bazel disk cache (default: ~/.cache/bazel/disk_cache)
#   GC_TMP_DIR         Directory that holds the test directories (default: /tmp)
set -euo pipefail

apply=false
go_days=3
tmp_hours=12
bazel_days=7

usage() {
	echo "usage: ${0##*/} [--apply] [--go-days N] [--tmp-hours N] [--bazel-days N]" >&2
	exit 2
}

number() {
	[[ "$1" =~ ^[0-9]+$ ]] || {
		echo "not a number: $1" >&2
		usage
	}
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	--apply) apply=true ;;
	--go-days | --tmp-hours | --bazel-days)
		[[ $# -ge 2 ]] || usage
		number "$2"
		case "$1" in
		--go-days) go_days=$2 ;;
		--tmp-hours) tmp_hours=$2 ;;
		--bazel-days) bazel_days=$2 ;;
		esac
		shift
		;;
	-h | --help) usage ;;
	*)
		echo "unknown argument: $1" >&2
		usage
		;;
	esac
	shift
done

if [[ "$(id -u)" == 0 ]]; then
	echo "run as the node operator, not as root" >&2
	exit 2
fi

go_cache="${GOCACHE:-$HOME/.cache/go-build}"
bazel_cache="${BAZEL_DISK_CACHE:-$HOME/.cache/bazel/disk_cache}"
tmp_root="${GC_TMP_DIR:-/tmp}"

human() {
	awk -v b="$1" 'BEGIN {
		split("B KiB MiB GiB TiB", u, " ")
		i = 1
		while (b >= 1024 && i < 5) { b /= 1024; i++ }
		printf "%.1f %s", b, u[i]
	}'
}

root_usage() {
	df -h --output=used,avail,pcent / | awk 'NR == 2 { print "used " $1 ", available " $2 ", " $3 }'
}

total_bytes=0
removed_bytes=0

mode=dry-run
$apply && mode=apply
echo "node-disk-gc: $mode"
echo "root filesystem before: $(root_usage)"

# The total size of the files in a null separated list.
file_bytes() {
	local list=$1
	[[ -s "$list" ]] || {
		echo 0
		return
	}
	xargs -0 stat -c %s <"$list" | awk '{ s += $1 } END { print s + 0 }'
}

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

report_files() {
	local label=$1 list=$2
	local count bytes
	count=$(tr -cd '\0' <"$list" | wc -c)
	bytes=$(file_bytes "$list")
	echo "$label: $count files, $(human "$bytes")"
	total_bytes=$((total_bytes + bytes))
	if $apply && [[ "$count" -gt 0 ]]; then
		xargs -0 rm -f <"$list"
		removed_bytes=$((removed_bytes + bytes))
	fi
}

# 1. Go build cache. Go refreshes a used entry's time, so an old file is unused.
if [[ "$go_cache" == /* && "$go_cache" != "$HOME" && -f "$go_cache/README" ]] &&
	grep -q "cached build artifacts from the Go build system" "$go_cache/README"; then
	find "$go_cache" -type f ! -name README ! -name trim.txt -mtime "+$go_days" -print0 >"$scratch/go"
	report_files "Go build cache $go_cache (older than $go_days days)" "$scratch/go"
else
	echo "Go build cache $go_cache: skipped (not a Go build cache)"
fi

# 2. Test directories. A path that any process holds open, or names in its
# command line (a postgres server names its data directory), stays. The match
# is a substring, so it can only keep too much.
held="$scratch/held"
: >"$held"
for proc in /proc/[0-9]*; do
	{
		readlink "$proc"/fd/* "$proc/cwd" 2>/dev/null || true
		tr '\0' '\n' 2>/dev/null <"$proc/cmdline" || true
	} >>"$held"
done
postgres="$scratch/postgres"
pgrep -a postgres >"$postgres" 2>/dev/null || true

in_use() {
	local dir=$1
	grep -qF -- "$dir" "$held" || grep -qF -- "$dir" "$postgres"
}

tmp_total=0
tmp_count=0
if [[ -d "$tmp_root" ]]; then
	tmp_real="$(cd "$tmp_root" && pwd -P)"
	while IFS= read -r -d '' dir; do
		[[ "$(dirname "$dir")" == "$tmp_root" ]] || continue
		[[ ! -L "$dir" ]] || continue
		if [[ -n "$(find "$dir" -mmin "-$((tmp_hours * 60))" -print -quit 2>/dev/null)" ]]; then
			echo "  keep (newer file): $dir"
			continue
		fi
		if in_use "$tmp_real/${dir##*/}" || in_use "$dir"; then
			echo "  keep (in use): $dir"
			continue
		fi
		size=$(du -sb "$dir" 2>/dev/null | awk '{ print $1 }')
		echo "  $dir $(human "$size")"
		tmp_count=$((tmp_count + 1))
		tmp_total=$((tmp_total + size))
		if $apply; then
			chmod -R u+w "$dir" && rm -rf "$dir"
			removed_bytes=$((removed_bytes + size))
		fi
	done < <(find "$tmp_root" -mindepth 1 -maxdepth 1 -type d -uid "$(id -u)" \
		\( -name 'pgdata*' -o -name 'tmp*' \) -mmin "+$((tmp_hours * 60))" -print0)
fi
echo "Test directories in $tmp_root (older than $tmp_hours h): $tmp_count, $(human "$tmp_total")"
total_bytes=$((total_bytes + tmp_total))

# 3. Bazel disk cache. Only ac/ and cas/ hold cache entries.
if [[ "$bazel_cache" == /* && "${bazel_cache##*/}" == disk_cache && -d "$bazel_cache" ]]; then
	: >"$scratch/bazel"
	for sub in ac cas; do
		if [[ -d "$bazel_cache/$sub" ]]; then
			find "$bazel_cache/$sub" -type f -mtime "+$bazel_days" -print0 >>"$scratch/bazel"
		fi
	done
	report_files "Bazel disk cache $bazel_cache (older than $bazel_days days)" "$scratch/bazel"
else
	echo "Bazel disk cache $bazel_cache: skipped (not a disk_cache directory)"
fi

echo "Reclaimable: $(human "$total_bytes")"
if $apply; then
	echo "Removed: $(human "$removed_bytes")"
	echo "root filesystem after: $(root_usage)"
else
	echo "Dry run. Run with --apply to remove them."
fi
