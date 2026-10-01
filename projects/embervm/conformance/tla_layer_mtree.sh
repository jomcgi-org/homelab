#!/usr/bin/env bash
# Emit the bsdtar mtree for the conformance image's /opt/tla layer (issue
# #6415): the pinned Temurin JRE, tla2tools.jar and adoption_trace.tla at the
# fixed in-image paths the chart's S6_TLC_* env points at.
#
#   /opt/tla/jre/...                  the JRE tree, relative layout preserved
#   /opt/tla/tla2tools.jar
#   /opt/tla/specs/adoption_trace.tla
#
# Every entry is owned by 0:0 and readable by the runner's uid 65532 (other).
# Mode is set by path, not read off the input, because remote execution marks
# every input executable: the JRE's own executables (bin/*, lib/jspawnhelper,
# lib/jexec, exactly the 0755 entries in the upstream tarball) get 0755,
# everything else 0644, directories 0755. The upstream tarball's symlinks all
# live under legal/ and are dereferenced here (Bazel stages them as files), so
# the layer carries no links.
#
# Args:
#   $1   the JRE's bin/java (its dir's parent is the JRE root)
#   $2   tla2tools.jar
#   $3   adoption_trace.tla
#   $4.. every file of the JRE filegroup
set -euo pipefail

java_anchor="$1"
jar="$2"
spec="$3"
shift 3

jre_root="${java_anchor%/bin/java}"
if [ "$jre_root" = "$java_anchor" ]; then
	echo "java anchor $java_anchor does not end in bin/java" >&2
	exit 1
fi

attrs="uid=0 gid=0 time=1672560000"
files=()
dirs=("./opt" "./opt/tla" "./opt/tla/jre" "./opt/tla/specs")

for f in "$@"; do
	rel="${f#"$jre_root"/}"
	if [ "$rel" = "$f" ]; then
		echo "JRE file $f is outside the JRE root $jre_root" >&2
		exit 1
	fi
	# Repo marker files Bazel writes next to the extracted archive.
	case "$rel" in
	BUILD | BUILD.bazel | REPO.bazel | WORKSPACE | WORKSPACE.bazel) continue ;;
	esac
	case "$rel" in
	bin/* | lib/jspawnhelper | lib/jexec) mode=0755 ;;
	*) mode=0644 ;;
	esac
	dest="./opt/tla/jre/$rel"
	files+=("$dest type=file mode=$mode $attrs content=$f")
	parent="${dest%/*}"
	while [ "$parent" != "./opt/tla/jre" ]; do
		dirs+=("$parent")
		parent="${parent%/*}"
	done
done

files+=("./opt/tla/tla2tools.jar type=file mode=0644 $attrs content=$jar")
files+=("./opt/tla/specs/adoption_trace.tla type=file mode=0644 $attrs content=$spec")

echo "#mtree"
# A parent sorts before its children, so every directory entry precedes what
# it holds.
printf '%s\n' "${dirs[@]}" | LC_ALL=C sort -u | while read -r d; do
	echo "$d type=dir mode=0755 $attrs"
done
printf '%s\n' "${files[@]}" | LC_ALL=C sort
