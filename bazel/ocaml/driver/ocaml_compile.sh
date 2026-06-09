#!/bin/sh
# Whole-library OCaml compile/link driver. The compiler is supplied as a single
# tar (the OCaml 5.3 `make install` prefix, built by //bazel/ocaml/toolchain:
# ocaml_compiler); the driver extracts it to a temp sysroot ($S). It recovers
# compile order with `ocamldep -sort`, compiles each module's .mli before its .ml,
# then archives the .cmx into a native .cmxa (library mode) or links a native
# executable (binary mode).
#
# The compiler binaries come from the extracted sysroot; native code generation
# and the final link use the execution host's as/gcc/ld (the same C toolchain the
# repo's C/C++ builds use). The sysroot's OCaml is relocated via OCAMLLIB.
set -eu

MODE="" NAME="" SYSROOT_TAR="" USE_FIND="0"
INCLUDES="" OPAM_PKGS="" SRCS="" CSRCS="" CMXAS="" CFLAGS=""
OBJS_OUT="" CMXA_OUT="" A_OUT="" EXE_OUT=""

while [ $# -gt 0 ]; do
	case "$1" in
	--mode) MODE="$2" && shift 2 ;;
	--name) NAME="$2" && shift 2 ;;
	--sysroot-tar) SYSROOT_TAR="$2" && shift 2 ;;
	--use-ocamlfind) USE_FIND="$2" && shift 2 ;;
	--compile-flag) CFLAGS="$CFLAGS $2" && shift 2 ;;
	--include) INCLUDES="$INCLUDES $2" && shift 2 ;;
	--opam-pkg) OPAM_PKGS="$OPAM_PKGS $2" && shift 2 ;;
	--src) SRCS="$SRCS $2" && shift 2 ;;
	--c-src) CSRCS="$CSRCS $2" && shift 2 ;;
	--cmxa) CMXAS="$CMXAS $2" && shift 2 ;;
	--objs-out) OBJS_OUT="$2" && shift 2 ;;
	--cmxa-out) CMXA_OUT="$2" && shift 2 ;;
	--a-out) A_OUT="$2" && shift 2 ;;
	--exe-out) EXE_OUT="$2" && shift 2 ;;
	*) echo "ocaml_compile: unknown arg: $1" >&2 && exit 2 ;;
	esac
done

# The sysroot tar is staged at an exec-root-relative path; make it absolute, then
# extract it to a fresh sysroot dir ($S) with the bin/ + lib/ocaml/ layout. A tar
# (single File artifact) survives RBE staging whole and preserves the +x bit,
# unlike a TreeArtifact of the install.
case "$SYSROOT_TAR" in
/*) TAR="$SYSROOT_TAR" ;;
*) TAR="$(pwd)/$SYSROOT_TAR" ;;
esac
S="$(mktemp -d)"
tar -xf "$TAR" -C "$S"

# --- Relocate the OCaml toolchain -------------------------------------------
# The compiler is built from source (semgrep/ocaml 5.3.0) with a baked-in
# --prefix; relocate it to wherever Bazel staged the sysroot via OCAMLLIB. The
# build configures plain `as`/`gcc` for native code generation and the final
# link, so those resolve from the execution host's PATH (the same C toolchain
# the repo's C/C++ builds use) — nothing is bundled.
export OCAMLLIB="$S/lib/ocaml"
export CAML_LD_LIBRARY_PATH="$S/lib/ocaml/stublibs${CAML_LD_LIBRARY_PATH:+:$CAML_LD_LIBRARY_PATH}"
export PATH="$S/bin:$PATH"

OCAMLOPT="$S/bin/ocamlopt.opt"
OCAMLDEP="$S/bin/ocamldep.opt"

echo "ocaml_compile: mode=$MODE sysroot=$S ocamlopt=$([ -x "$OCAMLOPT" ] && echo ok || echo MISSING) cc=$(command -v cc gcc 2>/dev/null | head -1) as=$(command -v as 2>/dev/null)" >&2

# Resolve opam (findlib) packages. ocamlfind is not part of the compiler; the
# toy's opam_deps (unix/str/threads) ship with the stdlib and link directly from
# OCAMLLIB by their archive name.
FIND=""
if [ "$USE_FIND" = "1" ] && [ -x "$S/bin/ocamlfind" ]; then
	FIND="$S/bin/ocamlfind"
fi

INCFLAGS=""
for d in $INCLUDES; do INCFLAGS="$INCFLAGS -I $d"; done

PKG_COMMA="" PKG_LINK=""
for p in $OPAM_PKGS; do
	if [ -z "$PKG_COMMA" ]; then PKG_COMMA="$p"; else PKG_COMMA="$PKG_COMMA,$p"; fi
	PKG_LINK="$PKG_LINK $p.cmxa"
done

# --- Stage sources into a writable work dir ---------------------------------
if [ "$MODE" = "library" ]; then
	WORK="$OBJS_OUT"
else
	WORK="$(mktemp -d)"
fi
mkdir -p "$WORK"
for s in $SRCS; do cp "$s" "$WORK/$(basename "$s")"; done
INCFLAGS="-I $WORK $INCFLAGS"

# --- Recover compile order over the .ml sources -----------------------------
MLB=""
for s in $SRCS; do
	case "$s" in *.ml) MLB="$MLB $(basename "$s")" ;; esac
done
ORDER="$(cd "$WORK" && "$OCAMLDEP" -sort $MLB)"
echo "ocaml_compile: compile order: $ORDER" >&2

# --- Compile each module (.mli before .ml) ----------------------------------
CMX_LIST=""
for ml in $ORDER; do
	base="${ml%.ml}"
	if [ -f "$WORK/$base.mli" ]; then
		"$OCAMLOPT" $CFLAGS $INCFLAGS -c "$WORK/$base.mli"
	fi
	"$OCAMLOPT" $CFLAGS $INCFLAGS -c "$WORK/$ml"
	CMX_LIST="$CMX_LIST $WORK/$base.cmx"
done

# --- Compile C stub sources (if any) ----------------------------------------
# ocamlopt compiles .c directly (it supplies caml/*.h) using the execution
# host's C compiler; the .o lands next to the source in $WORK.
STUB_OBJS=""
for c in $CSRCS; do
	cb="$(basename "$c")"
	cp "$c" "$WORK/$cb"
	(cd "$WORK" && "$OCAMLOPT" -c "$cb")
	STUB_OBJS="$STUB_OBJS $WORK/${cb%.c}.o"
done

# --- Produce the output -----------------------------------------------------
if [ "$MODE" = "library" ]; then
	# ocamlopt -o NAME.cmxa also writes NAME.a alongside it.
	"$OCAMLOPT" -a -o "$CMXA_OUT" $CMX_LIST
	[ "$A_OUT" = "${CMXA_OUT%.cmxa}.a" ] || cp "${CMXA_OUT%.cmxa}.a" "$A_OUT"
	# Fold C stub objects into the library archive (the .a ocamlopt auto-finds
	# next to the .cmxa), so binaries linking this library resolve the externals.
	# Use an explicit if (not `test && ar`): with no c_srcs the && list would
	# return non-zero and trip `set -e`.
	if [ -n "$STUB_OBJS" ]; then
		ar r "${CMXA_OUT%.cmxa}.a" $STUB_OBJS
	fi
else
	# Link order: stdlib opam archives, then deps (postorder), own modules, then
	# any C stub objects compiled for this binary directly.
	"$OCAMLOPT" $CFLAGS $INCFLAGS $PKG_LINK $CMXAS $CMX_LIST $STUB_OBJS -o "$EXE_OUT"
fi
