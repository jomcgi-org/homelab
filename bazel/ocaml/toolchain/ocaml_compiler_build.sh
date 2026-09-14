#!/usr/bin/env bash
# Build the pinned OCaml compiler with only declared, checksum-locked tools.
set -eu

OUT_REL="$1"
SRC_REL="$2"
ZIG_REL="$3"
ROOTFS_REL="$4"
MAKE_REL="$5"
TOYBOX_REL="$6"
SHELL_REL="$7"

EXEC_ROOT="$PWD"
OUT="$EXEC_ROOT/$OUT_REL"
SRC="$EXEC_ROOT/$SRC_REL"
ZIG_ARCHIVE="$EXEC_ROOT/$ZIG_REL"
ROOTFS_ARCHIVE="$EXEC_ROOT/$ROOTFS_REL"
MAKE_APK="$EXEC_ROOT/$MAKE_REL"
TOYBOX="$EXEC_ROOT/$TOYBOX_REL"
SHELL_INPUT="$EXEC_ROOT/$SHELL_REL"

WORK="$($TOYBOX mktemp -d)"
NATIVE="$WORK/native"
HBIN="$NATIVE/bin"
$TOYBOX mkdir -p "$HBIN" "$NATIVE/rootfs" "$NATIVE/zig"
$TOYBOX tar -xJf "$ZIG_ARCHIVE" -C "$NATIVE/zig" --strip-components=1
$TOYBOX tar -xzf "$ROOTFS_ARCHIVE" -C "$NATIVE/rootfs"
$TOYBOX tar -xzf "$MAKE_APK" -C "$NATIVE/rootfs"
$TOYBOX cp "$SHELL_INPUT" "$HBIN/bash"
$TOYBOX cp "$TOYBOX" "$HBIN/toybox"

case "$($TOYBOX uname -m)" in
x86_64) MUSL_ARCH=x86_64 ;;
aarch64) MUSL_ARCH=aarch64 ;;
*)
	echo "ocaml_compiler: unsupported executor architecture" >&2
	exit 2
	;;
esac
LOADER="$NATIVE/rootfs/lib/ld-musl-$MUSL_ARCH.so.1"
BUSYBOX="$NATIVE/rootfs/bin/busybox"

# BusyBox provides the configure/build utilities. Each generated wrapper uses
# the pinned static Bash as its interpreter and the staged Alpine loader.
for applet in $("$LOADER" --library-path "$NATIVE/rootfs/lib" "$BUSYBOX" --list); do
	case "$applet" in
	sh | bash | tsort | file | cc | gcc | clang | ar | ranlib | nm | objcopy | strip | as | make | zig) continue ;;
	esac
	printf '%s\n' "#!$HBIN/bash" \
		"exec \"$LOADER\" --library-path \"$NATIVE/rootfs/lib\" \"$BUSYBOX\" $applet \"\$@\"" \
		>"$HBIN/$applet"
	"$TOYBOX" chmod +x "$HBIN/$applet"
done
$TOYBOX ln -s "$HBIN/bash" "$HBIN/sh"
$TOYBOX ln -s "$HBIN/toybox" "$HBIN/tsort"
$TOYBOX ln -s "$HBIN/toybox" "$HBIN/file"
$TOYBOX ln -s "$NATIVE/zig/zig" "$HBIN/zig"

for tool in cc gcc clang; do
	printf '%s\n' "#!$HBIN/bash" "exec \"$HBIN/zig\" cc \"\$@\"" >"$HBIN/$tool"
	"$TOYBOX" chmod +x "$HBIN/$tool"
done
for tool in ar ranlib nm objcopy; do
	printf '%s\n' "#!$HBIN/bash" "exec \"$HBIN/zig\" $tool \"\$@\"" >"$HBIN/$tool"
	"$TOYBOX" chmod +x "$HBIN/$tool"
done
printf '%s\n' "#!$HBIN/bash" "exec \"$HBIN/zig\" cc -c \"\$@\"" >"$HBIN/as"
printf '%s\n' "#!$HBIN/bash" \
	"exec \"$LOADER\" --library-path \"$NATIVE/rootfs/lib\" \"$NATIVE/rootfs/usr/bin/make\" \"\$@\"" \
	>"$HBIN/make"
$TOYBOX chmod +x "$HBIN/as" "$HBIN/make"

export PATH="$HBIN"
$TOYBOX mkdir -p "$WORK/src"
$TOYBOX cp -RL "$SRC/." "$WORK/src"
cd "$WORK/src"
$TOYBOX chmod +x configure

log() { "$TOYBOX" tail -80 "$1" >&2; }
"$HBIN/bash" ./configure \
	--prefix="$WORK/_install" \
	CC=cc AS=as ASPP="cc -c" PARTIALLD="cc -r" AR=ar RANLIB=ranlib \
	STRIP=true >_cfg.log 2>&1 || {
	log _cfg.log
	exit 1
}
"$HBIN/make" -j"$($TOYBOX nproc)" SHELL="$HBIN/bash" MAKE="$HBIN/make" \
	>_make.log 2>&1 || {
	log _make.log
	exit 1
}
"$HBIN/make" install SHELL="$HBIN/bash" MAKE="$HBIN/make" \
	>_inst.log 2>&1 || {
	log _inst.log
	exit 1
}

$TOYBOX test -x "$WORK/_install/bin/ocamlopt.opt"
$TOYBOX test -f "$WORK/_install/lib/ocaml/compiler-libs/ocamlcommon.cmxa"

# Carry a clean copy of the exact tools into every consuming action. Runtime
# wrappers are recreated after extraction so they never contain stale paths.
$TOYBOX mkdir -p "$WORK/_install/native/bin"
$TOYBOX cp "$ZIG_ARCHIVE" "$WORK/_install/native/zig.tar.xz"
$TOYBOX cp "$ROOTFS_ARCHIVE" "$WORK/_install/native/rootfs.tar.gz"
$TOYBOX cp "$HBIN/bash" "$WORK/_install/native/bin/bash"
$TOYBOX cp "$HBIN/toybox" "$WORK/_install/native/bin/toybox"
$TOYBOX uname -m >"$WORK/_install/.ocaml-sysroot-arch"
echo "ocaml_compiler: built pinned native sysroot on $($TOYBOX uname -m)" >&2
$TOYBOX tar -cf "$OUT" -C "$WORK/_install" .
