#!/usr/bin/env bash
# Garbage-collects stale entries from the Bazel repository cache.
#
# Bazel 9 has built-in GC for disk_cache and repo_contents_cache, but the
# content-addressable download cache (content_addressable/sha256/) has no
# automatic cleanup. This script prunes entries not accessed in MAX_AGE_DAYS.
#
# Usage:
#   bazel/tools/bazel-cache-gc.sh              # dry-run (default)
#   bazel/tools/bazel-cache-gc.sh --delete     # actually delete stale entries
#
# Recommended: run monthly or when disk usage gets high.
set -o errexit -o nounset -o pipefail

CACHE_DIR="${BAZEL_REPO_CACHE:-${HOME}/.cache/bazel/repository_cache}"
MAX_AGE_DAYS="${BAZEL_CACHE_MAX_AGE_DAYS:-30}"
DRY_RUN=true

for arg in "$@"; do
	case "$arg" in
	--delete) DRY_RUN=false ;;
	--help | -h)
		echo "Usage: $0 [--delete]"
		echo "  --delete  Actually remove stale cache entries (default: dry-run)"
		exit 0
		;;
	*)
		echo "Unknown argument: $arg" >&2
		exit 1
		;;
	esac
done

if [ ! -d "$CACHE_DIR" ]; then
	echo "Cache directory not found: $CACHE_DIR"
	exit 0
fi

# Report current usage
echo "Repository cache: $CACHE_DIR"
du -sh "$CACHE_DIR" 2>/dev/null | awk '{print "Current size: " $1}'
echo "Max age: ${MAX_AGE_DAYS} days"
echo ""

# Find stale entries in content_addressable (the unbounded part)
ca_dir="$CACHE_DIR/content_addressable"
if [ ! -d "$ca_dir" ]; then
	echo "No content_addressable directory found, nothing to clean."
	exit 0
fi

# Use -atime on macOS (last access time) to avoid evicting files still being
# referenced by hardlinks in active output bases.
stale_files=$(find "$ca_dir" -type f -atime "+${MAX_AGE_DAYS}" 2>/dev/null)
stale_count=$(echo "$stale_files" | grep -c . 2>/dev/null || echo 0)

if [ "$stale_count" -eq 0 ]; then
	echo "No stale entries older than ${MAX_AGE_DAYS} days."
	exit 0
fi

# Calculate reclaimable space
stale_size=$(echo "$stale_files" | xargs du -ch 2>/dev/null | tail -1 | awk '{print $1}')
echo "Stale entries: $stale_count files ($stale_size)"

if [ "$DRY_RUN" = true ]; then
	echo ""
	echo "Dry-run mode. Run with --delete to remove these entries."
else
	echo "Deleting stale entries..."
	echo "$stale_files" | xargs rm -f
	# Clean up empty directories
	find "$ca_dir" -type d -empty -delete 2>/dev/null || true
	echo "Done. Reclaimed $stale_size."
fi
