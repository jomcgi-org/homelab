#!/usr/bin/env bash
# Removes build and test leftovers from a workstation node.
#
# On 2026-11-23 these leftovers filled node-1's root filesystem. The kubelet
# then tainted the node for disk pressure and left 22 pods Pending. The script
# lists and, with --apply, removes only these three kinds of file:
#
#   1. Go build cache files not modified for --go-days (default 3), under
#      GOCACHE. The directory must hold Go's README marker. The fuzz corpus
#      (fuzz/) stays. Symlinks are not followed.
#   2. Test directories /tmp/pgdata* and /tmp/tmp* (direct children of
#      GC_TMP_DIR, owned by the caller, not symlinks) that are older than
#      --tmp-hours (default 12) and hold no newer file. A directory stays if
#      any of these name it, in any process the caller can read:
#        - an open file, the working directory or the executable
#        - a mapped file (/proc/PID/maps), such as a library
#        - a command-line argument or an environment value (TMPDIR=...)
#        - a bound unix socket path (/proc/net/unix)
#      A directory also stays if a postgres process names it, if a mount
#      point lies under it, or if its name holds a newline. The scan runs again
#      right before each removal. Removal never crosses a mount point.
#      GC_TMP_DIR is resolved to its real path first, so a symlinked path
#      cannot hide a mount point. A subdirectory that the caller cannot read
#      is opened with chmod before the age check (only with --apply), and a
#      directory that stays unreadable is kept.
#      Known gap: a process that holds a directory only by a relative path (a
#      unix socket bound with a relative path, a relative argument after a
#      chdir) is not seen.
#   3. Bazel disk cache files (ac/ and cas/ only) not modified for
#      --bazel-days (default 7), under BAZEL_DISK_CACHE.
#
# It touches nothing else. It does not install a timer. Dry run is the default.
# It refuses to run as root, unless GOCACHE, BAZEL_DISK_CACHE and GC_TMP_DIR are
# all set and GC_TMP_DIR is not /tmp. A root run then touches only those three
# paths, and GC_TMP_DIR matches directories that root owns.
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

# A whole number without leading zeros (bash reads 08 as an octal error).
number() {
	[[ "$1" =~ ^(0|[1-9][0-9]*)$ ]] || {
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
		--tmp-hours)
			[[ "$2" != 0 ]] || {
				echo "--tmp-hours must be at least 1" >&2
				usage
			}
			tmp_hours=$2
			;;
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

go_cache="${GOCACHE:-$HOME/.cache/go-build}"
bazel_cache="${BAZEL_DISK_CACHE:-$HOME/.cache/bazel/disk_cache}"
tmp_root="${GC_TMP_DIR:-/tmp}"
mountinfo=/proc/self/mountinfo

# The directory that holds the test directories: an absolute path with no
# trailing slash.
if [[ "$tmp_root" != /* || "$tmp_root" == / || "$tmp_root" == */ ]]; then
	echo "GC_TMP_DIR must be an absolute path without a trailing slash, and not /: $tmp_root" >&2
	exit 2
fi

realdir() {
	(cd "$1" 2>/dev/null && pwd -P)
}

# Real paths: a symlink in a path must not hide a mount point, and the checks
# on HOME must see through one.
home_real="$(realdir "$HOME" || true)"
tmp_real="$(realdir "$tmp_root" || true)"

# Neither / nor the home directory nor a directory that holds it.
holds_home() {
	[[ -n "$home_real" && ("$1" == / || "$1" == "$home_real" || "$home_real" == "$1"/*) ]]
}

if [[ -n "$tmp_real" ]] && holds_home "$tmp_real"; then
	echo "GC_TMP_DIR must not be / or the home directory or a directory that holds it: $tmp_root" >&2
	exit 2
fi

# From here on, every path in the test directories is a real path.
[[ -z "$tmp_real" ]] || tmp_root=$tmp_real

if [[ "$(id -u)" == 0 ]]; then
	if [[ -z "${GOCACHE:-}" || -z "${BAZEL_DISK_CACHE:-}" || -z "${GC_TMP_DIR:-}" || "$tmp_real" == /tmp || -z "$tmp_real" ]]; then
		echo "run as the node operator, not as root (a root run needs GOCACHE, BAZEL_DISK_CACHE and GC_TMP_DIR set, and GC_TMP_DIR not /tmp)" >&2
		exit 2
	fi
	echo "running as root on $tmp_real, $go_cache and $bazel_cache only"
fi

# The checks below read /proc and the mount table. Without them the script
# cannot tell what is in use, so it stops.
if [[ ! -r "$mountinfo" || ! -r /proc/self/maps || ! -r /proc/net/unix ]]; then
	echo "cannot read /proc or the mount table: $mountinfo" >&2
	exit 2
fi

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

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# The total size of the files in a null separated list. A file that vanished
# since the list was made counts as 0.
file_bytes() {
	local list=$1
	[[ -s "$list" ]] || {
		echo 0
		return
	}
	{ xargs -0 stat -c %s 2>/dev/null <"$list" || true; } | awk '{ s += $1 } END { print s + 0 }'
}

report_files() {
	local label=$1 list=$2
	local count bytes
	count=$(tr -cd '\0' <"$list" | wc -c)
	bytes=$(file_bytes "$list")
	echo "$label: $count files, $(human "$bytes")"
	total_bytes=$((total_bytes + bytes))
	if $apply && [[ "$count" -gt 0 ]]; then
		if xargs -0 rm -f <"$list"; then
			removed_bytes=$((removed_bytes + bytes))
		else
			echo "  failed: some files in $label"
		fi
	fi
}

# 1. Go build cache. Go refreshes a used entry's time, so an old file is unused.
# The path loses its trailing slashes, and it must not be the home directory or
# a directory that holds it.
while [[ "$go_cache" == */ ]]; do go_cache=${go_cache%/}; done
go_real="$(realdir "$go_cache" || true)"
if [[ "$go_cache" == /* && -n "$go_real" && -f "$go_real/README" ]] && ! holds_home "$go_real" &&
	grep -q "cached build artifacts from the Go build system" "$go_real/README"; then
	find "$go_real" -xdev -type f ! -name README ! -name trim.txt ! -path "$go_real/fuzz/*" \
		-mmin "+$((go_days * 1440))" -print0 >"$scratch/go" 2>/dev/null || true
	report_files "Go build cache $go_real (older than $go_days days)" "$scratch/go"
else
	echo "Go build cache $go_cache: skipped (not a Go build cache, or it holds the home directory)"
fi

# 2. Test directories.
#
# held holds one line for each path that a process uses. The match is a
# substring match, so it can only keep too much. A newline inside a path splits
# it into two lines, which is why a directory name with a newline stays.
held="$scratch/held"
postgres="$scratch/postgres"
held_at=0

scan() {
	: >"$held"
	local proc
	for proc in /proc/[0-9]*; do
		{
			readlink "$proc"/fd/* "$proc/cwd" "$proc/exe" 2>/dev/null || true
			# Mapped files: the sixth column onward of /proc/PID/maps.
			awk '{ $1 = $2 = $3 = $4 = $5 = ""; sub(/^ +/, ""); if ($0 != "") print }' "$proc/maps" 2>/dev/null || true
			tr '\0' '\n' 2>/dev/null <"$proc/cmdline" || true
			tr '\0' '\n' 2>/dev/null <"$proc/environ" || true
		} >>"$held"
	done
	# Bound unix sockets show as socket:[inode] in fd. Their path is the
	# eighth column of /proc/net/unix.
	cut -d' ' -f8- /proc/net/unix 2>/dev/null >>"$held" || true
	pgrep -a postgres >"$postgres" 2>/dev/null || true
	held_at=$SECONDS
}

# Scan again when the last scan is older than 2 s.
rescan() {
	if [[ $((SECONDS - held_at)) -ge 2 ]]; then
		scan
	fi
}

in_use() {
	local dir=$1
	grep -qF -- "$dir" "$held" || grep -qF -- "$dir" "$postgres"
}

# Succeeds when a mount point is the directory or lies under it. Field 5 of
# the mount table is the mount point; the kernel writes a space, tab, newline
# or backslash in it as an octal escape.
has_mount() {
	DIR=$1 awk '
		function unescape(s,   out, n) {
			out = ""
			while (match(s, /\\[0-7][0-7][0-7]/)) {
				n = substr(s, RSTART + 1, 1) * 64 + substr(s, RSTART + 2, 1) * 8 + substr(s, RSTART + 3, 1)
				out = out substr(s, 1, RSTART - 1) sprintf("%c", n)
				s = substr(s, RSTART + RLENGTH)
			}
			return out s
		}
		BEGIN { dir = ENVIRON["DIR"] }
		{
			m = unescape($5)
			if (m == dir || index(m, dir "/") == 1) { found = 1; exit }
		}
		END { exit !found }
	' "$mountinfo"
}

# Prints the reason that a directory must stay, or nothing.
keep_reason() {
	local dir=$1
	if has_mount "$dir"; then
		echo "mount"
	elif in_use "$dir"; then
		echo "in use"
	fi
}

tmp_total=0
tmp_count=0
if [[ -n "$tmp_real" ]]; then
	scan
	while IFS= read -r -d '' dir; do
		[[ "$(dirname "$dir")" == "$tmp_root" ]] || continue
		[[ ! -L "$dir" ]] || continue
		[[ "$dir" != "$scratch" ]] || continue
		if [[ "$dir" == *$'\n'* ]]; then
			printf '  keep (name has a newline): %q\n' "$dir"
			continue
		fi
		reason=$(keep_reason "$dir")
		if [[ -n "$reason" ]]; then
			echo "  keep ($reason): $dir"
			continue
		fi
		# A subdirectory that the caller cannot read hides its files from the
		# age check. With --apply, chmod opens it and the check runs again.
		newer=$(find "$dir" -mmin "-$((tmp_hours * 60))" -print -quit 2>"$scratch/err" || true)
		if [[ -s "$scratch/err" && -z "$newer" ]]; then
			if ! $apply; then
				echo "  keep (unreadable; --apply opens it first): $dir"
				continue
			fi
			chmod -R u+rwX -- "$dir" 2>/dev/null || true
			newer=$(find "$dir" -mmin "-$((tmp_hours * 60))" -print -quit 2>"$scratch/err" || true)
			if [[ -s "$scratch/err" && -z "$newer" ]]; then
				echo "  keep (unreadable): $dir"
				continue
			fi
		fi
		if [[ -n "$newer" ]]; then
			echo "  keep (newer file): $dir"
			continue
		fi
		size=$(du -sb "$dir" 2>/dev/null | awk '{ print $1 }' || true)
		size=${size:-0}
		echo "  $dir $(human "$size")"
		tmp_count=$((tmp_count + 1))
		tmp_total=$((tmp_total + size))
		if $apply; then
			# The scan is a snapshot from before du and any earlier removal.
			rescan
			reason=$(keep_reason "$dir")
			if [[ -n "$reason" ]]; then
				echo "  keep ($reason): $dir"
				continue
			fi
			# Test databases make files read-only. u+rwX also opens an
			# unreadable subdirectory so that rm can enter it.
			chmod -R u+rwX -- "$dir" 2>/dev/null || true
			if rm -rf --one-file-system -- "$dir" 2>/dev/null && [[ ! -e "$dir" ]]; then
				removed_bytes=$((removed_bytes + size))
			else
				echo "  failed: $dir"
			fi
		fi
	done < <(find "$tmp_root" -mindepth 1 -maxdepth 1 -type d -uid "$(id -u)" \
		\( -name 'pgdata*' -o -name 'tmp*' \) -mmin "+$((tmp_hours * 60))" -print0 2>/dev/null)
fi
echo "Test directories in $tmp_root (older than $tmp_hours h): $tmp_count, $(human "$tmp_total")"
total_bytes=$((total_bytes + tmp_total))

# 3. Bazel disk cache. Only ac/ and cas/ hold cache entries.
if [[ "$bazel_cache" == /* && "${bazel_cache##*/}" == disk_cache && -d "$bazel_cache" ]]; then
	: >"$scratch/bazel"
	for sub in ac cas; do
		if [[ -d "$bazel_cache/$sub" ]]; then
			find "$bazel_cache/$sub" -xdev -type f -mmin "+$((bazel_days * 1440))" -print0 >>"$scratch/bazel" 2>/dev/null || true
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
