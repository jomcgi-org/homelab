#!/bin/sh
# Whole-library OCaml compile/link driver. The compiler is supplied as a single
# tar (the OCaml 5.3 `make install` prefix, built by //bazel/ocaml/toolchain:
# ocaml_compiler); the driver extracts it to a temp sysroot ($S). It stages the
# sources into a work dir, runs the source-generation pipeline (ocamlyacc,
# ocamllex, cppo, a generic per-file preprocessor, a ppx driver), recovers
# compile order with `ocamldep -sort`, optionally applies dune-style module
# wrapping, then archives the .cmx into a native .cmxa (library mode) or links
# a native executable (binary mode).
#
# The compiler binaries come from the extracted sysroot; native code generation
# and the final link use the execution host's as/gcc/ld (the same C toolchain the
# repo's C/C++ builds use). The sysroot's OCaml is relocated via OCAMLLIB.
set -eu

MODE="" NAME="" SYSROOT_TAR="" USE_FIND="0" WRAPPED="0" LINKALL="0"
INCLUDES="" OPAM_PKGS="" SRCS="" CSRCS="" CMXAS="" CFLAGS=""
PP_TOOL="" PP_ARGS="" CPPO_TOOL="" PPX=""
MENHIR_TOOL="" MENHIR_MODULES="" MENHIR_FLAGS=""
CC_INCLUDES="" CC_ARCHIVES="" CC_LINKFLAGS=""
OBJS_OUT="" CMXA_OUT="" A_OUT="" EXE_OUT=""

while [ $# -gt 0 ]; do
	case "$1" in
	--mode) MODE="$2" && shift 2 ;;
	--name) NAME="$2" && shift 2 ;;
	--sysroot-tar) SYSROOT_TAR="$2" && shift 2 ;;
	--use-ocamlfind) USE_FIND="$2" && shift 2 ;;
	--wrapped) WRAPPED="$2" && shift 2 ;;
	--linkall) LINKALL="$2" && shift 2 ;;
	--compile-flag) CFLAGS="$CFLAGS $2" && shift 2 ;;
	--include) INCLUDES="$INCLUDES $2" && shift 2 ;;
	--opam-pkg) OPAM_PKGS="$OPAM_PKGS $2" && shift 2 ;;
	--src) SRCS="$SRCS $2" && shift 2 ;;
	--c-src) CSRCS="$CSRCS $2" && shift 2 ;;
	--cmxa) CMXAS="$CMXAS $2" && shift 2 ;;
	--pp-tool) PP_TOOL="$2" && shift 2 ;;
	--pp-arg) PP_ARGS="$PP_ARGS $2" && shift 2 ;;
	--cppo-tool) CPPO_TOOL="$2" && shift 2 ;;
	--ppx) PPX="$2" && shift 2 ;;
	--menhir-tool) MENHIR_TOOL="$2" && shift 2 ;;
	--menhir-module) MENHIR_MODULES="$MENHIR_MODULES $2" && shift 2 ;;
	--menhir-flag) MENHIR_FLAGS="$MENHIR_FLAGS $2" && shift 2 ;;
	--cc-include) CC_INCLUDES="$CC_INCLUDES $2" && shift 2 ;;
	--cc-archive) CC_ARCHIVES="$CC_ARCHIVES $2" && shift 2 ;;
	--cc-linkflag) CC_LINKFLAGS="$CC_LINKFLAGS $2" && shift 2 ;;
	--objs-out) OBJS_OUT="$2" && shift 2 ;;
	--cmxa-out) CMXA_OUT="$2" && shift 2 ;;
	--a-out) A_OUT="$2" && shift 2 ;;
	--exe-out) EXE_OUT="$2" && shift 2 ;;
	*) echo "ocaml_compile: unknown arg: $1" >&2 && exit 2 ;;
	esac
done

# Inputs are staged at exec-root-relative paths; later steps cd around, so
# resolve anything we execute or read from another directory to an absolute path.
abspath() {
	case "$1" in
	/*) printf '%s' "$1" ;;
	*) printf '%s/%s' "$(pwd)" "$1" ;;
	esac
}

# The sysroot tar is extracted to a fresh sysroot dir ($S) with the bin/ +
# lib/ocaml/ layout. A tar (single File artifact) survives RBE staging whole and
# preserves the +x bit, unlike a TreeArtifact of the install.
TAR="$(abspath "$SYSROOT_TAR")"
S="$(mktemp -d)"
TMP_WORK=""
trap 'rm -rf "$S" $TMP_WORK' EXIT
tar -xf "$TAR" -C "$S"

[ -n "$PP_TOOL" ] && PP_TOOL="$(abspath "$PP_TOOL")"
[ -n "$CPPO_TOOL" ] && CPPO_TOOL="$(abspath "$CPPO_TOOL")"
[ -n "$PPX" ] && PPX="$(abspath "$PPX")"
[ -n "$MENHIR_TOOL" ] && MENHIR_TOOL="$(abspath "$MENHIR_TOOL")"

# C library integration (cc_deps): absolute -I for the stub compile (which cds
# into the work dir) and absolute archive paths for the final link.
CCOPT_INC=""
for d in $CC_INCLUDES; do CCOPT_INC="$CCOPT_INC -ccopt -I$(abspath "$d")"; done
CC_ARCH_ABS=""
for a in $CC_ARCHIVES; do CC_ARCH_ABS="$CC_ARCH_ABS $(abspath "$a")"; done
CC_CCLIB=""
for fl in $CC_LINKFLAGS; do CC_CCLIB="$CC_CCLIB -cclib $fl"; done

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
OCAMLLEX="$S/bin/ocamllex"
OCAMLYACC="$S/bin/ocamlyacc"

# Per-arch sysroots are built on the arch they target (ADR 006). If the
# scheduler hands this action to an executor of a different arch than the one
# that built the staged sysroot, every tool invocation fails as a cryptic
# "not found" (the foreign ELF interpreter is missing), so assert it up front
# with a message that names the mismatch.
EXEC_ARCH="$(uname -m)"
SYSROOT_ARCH="$(cat "$S/.ocaml-sysroot-arch" 2>/dev/null || echo unknown)"
if [ "$SYSROOT_ARCH" != "unknown" ] && [ "$SYSROOT_ARCH" != "$EXEC_ARCH" ]; then
	echo "ocaml_compile: FATAL: sysroot was built on $SYSROOT_ARCH but this action is executing on $EXEC_ARCH (executor pool routing mismatch)" >&2
	exit 1
fi

echo "ocaml_compile: mode=$MODE arch=$EXEC_ARCH sysroot=$S ocamlopt=$([ -x "$OCAMLOPT" ] && echo ok || echo MISSING) cc=$(command -v cc gcc 2>/dev/null | head -1) as=$(command -v as 2>/dev/null)" >&2

# Version tokens for preprocessor arg substitution (see --pp-arg below).
# %OCAML_VERSION% is the numeric version (5.3.0); %OCAML_AST_VERSION% is
# ppxlib's astlib token (503), with the 5.0 -> 414 quirk from
# astlib/config/gen.ml (the AST did not change between 4.14 and 5.0).
# The semgrep fork reports "5.3.0+semgrep-fork@<sha>"; strip everything from
# the first non-numeric character so cppo and version compares stay parseable.
OCAML_VERSION="$("$OCAMLOPT" -version | sed 's/[^0-9.].*//')"
_maj="${OCAML_VERSION%%.*}"
_rest="${OCAML_VERSION#*.}"
_min="${_rest%%.*}"
if [ "$_maj" = "5" ] && [ "$_min" = "0" ]; then
	OCAML_AST_VERSION="414"
else
	OCAML_AST_VERSION="$(printf '%d%02d' "$_maj" "$_min")"
fi

# Resolve opam (findlib) packages against the sysroot. Plain names are
# stdlib-shipped archives (unix, str, threads...); since OCaml 5 those live in
# lib/ocaml/<pkg>/ subdirs, hence the guarded -I +<pkg>. Dotted compiler-libs
# names map onto the archives a from-source `make install` ships under
# +compiler-libs (this is what lets ocaml-compiler-libs and astlib build).
FIND=""
if [ "$USE_FIND" = "1" ] && [ -x "$S/bin/ocamlfind" ]; then
	FIND="$S/bin/ocamlfind"
fi

PKG_INC="" PKG_LINK=""
add_pkg_link() {
	case " $PKG_LINK " in
	*" $1 "*) ;;
	*) PKG_LINK="$PKG_LINK $1" ;;
	esac
}
for p in $OPAM_PKGS; do
	case "$p" in
	compiler-libs.common)
		PKG_INC="$PKG_INC -I +compiler-libs"
		add_pkg_link ocamlcommon.cmxa
		;;
	compiler-libs.bytecomp)
		PKG_INC="$PKG_INC -I +compiler-libs"
		add_pkg_link ocamlcommon.cmxa
		add_pkg_link ocamlbytecomp.cmxa
		;;
	compiler-libs.optcomp)
		PKG_INC="$PKG_INC -I +compiler-libs"
		add_pkg_link ocamlcommon.cmxa
		add_pkg_link ocamloptcomp.cmxa
		;;
	compiler-libs*)
		echo "ocaml_compile: unsupported compiler-libs sublibrary: $p" >&2
		exit 2
		;;
	*)
		if [ -d "$OCAMLLIB/$p" ]; then
			PKG_INC="$PKG_INC -I +$p"
		fi
		add_pkg_link "$p.cmxa"
		;;
	esac
done

INCFLAGS=""
for d in $INCLUDES; do INCFLAGS="$INCFLAGS -I $d"; done
INCFLAGS="$INCFLAGS$PKG_INC"

# --- Stage sources into a writable work dir ---------------------------------
if [ "$MODE" = "library" ]; then
	WORK="$OBJS_OUT"
else
	WORK="$(mktemp -d)"
	TMP_WORK="$WORK"
fi
mkdir -p "$WORK"
for s in $SRCS; do cp "$s" "$WORK/$(basename "$s")"; done
INCFLAGS="-I $WORK $INCFLAGS"

# --- Source generation pipeline ----------------------------------------------
# Mirrors the dune stanzas the opam universe needs, in dune's order:
#   .mly  -> ocamlyacc        (dune `(ocamlyacc x)`) unless the module is a
#                             menhir grammar (handled with type inference below)
#   .mll  -> ocamllex         (dune `(ocamllex x)`)
#   x.cppo.ml{,i} -> x.ml{,i} (the cppo `(rule ...)` convention)
#   --menhir-module           (dune `(menhir (modules ...))`, with --infer)
#   --pp-tool                 (dune `(preprocess (action (run tool args file)))`,
#                              output on stdout; args may use %OCAML_VERSION% /
#                              %OCAML_AST_VERSION%)
#   --ppx                     (a ppxlib standalone driver; rewrites in place)
is_menhir_module() {
	for _m in $MENHIR_MODULES; do [ "$_m" = "$1" ] && return 0; done
	return 1
}
for f in "$WORK"/*.mly; do
	[ -e "$f" ] || continue
	b="$(basename "$f" .mly)"
	# menhir grammars are generated by the menhir pass below, not ocamlyacc.
	is_menhir_module "$b" && continue
	(cd "$WORK" && "$OCAMLYACC" "$(basename "$f")")
	rm "$f"
done
for f in "$WORK"/*.mll; do
	[ -e "$f" ] || continue
	(cd "$WORK" && "$OCAMLLEX" -q "$(basename "$f")")
	rm "$f"
done
if [ -n "$CPPO_TOOL" ]; then
	for f in "$WORK"/*.cppo.ml "$WORK"/*.cppo.mli; do
		[ -e "$f" ] || continue
		ext="${f##*.}"
		out="${f%.cppo.$ext}.$ext"
		"$CPPO_TOOL" -V "OCAML:$OCAML_VERSION" "$f" -o "$out"
		rm "$f"
	done
fi
if [ -n "$PP_TOOL" ]; then
	SUBST_ARGS=""
	for a in $PP_ARGS; do
		a="$(printf '%s' "$a" | sed "s/%OCAML_VERSION%/$OCAML_VERSION/g; s/%OCAML_AST_VERSION%/$OCAML_AST_VERSION/g")"
		SUBST_ARGS="$SUBST_ARGS $a"
	done
	for f in "$WORK"/*.ml "$WORK"/*.mli; do
		[ -e "$f" ] || continue
		"$PP_TOOL" $SUBST_ARGS "$f" >"$f.pp"
		mv "$f.pp" "$f"
	done
fi
if [ -n "$PPX" ]; then
	# ppx output is source-compatible OCaml; rewriting in place keeps file/unit
	# names stable so wrapping and ocamldep are untouched downstream.
	for f in "$WORK"/*.ml; do
		[ -e "$f" ] || continue
		"$PPX" --impl "$f" -o "$f.pp"
		mv "$f.pp" "$f"
	done
	for f in "$WORK"/*.mli; do
		[ -e "$f" ] || continue
		"$PPX" --intf "$f" -o "$f.pp"
		mv "$f.pp" "$f"
	done
fi

# --- menhir grammars (with OCaml type inference) -----------------------------
# menhir's --infer protocol needs the OCaml types of the semantic actions. We
# (1) compile the library's other modules into a scratch dir (best effort;
# modules that themselves need the parser fail there and are skipped -- they are
# not needed to type the grammar), (2) write an inference query and compile it
# for its signature, (3) read the reply back to generate <M>.ml / <M>.mli. The
# generated parser then flows through the normal ocamldep/compile pipeline below.
if [ -n "$MENHIR_MODULES" ]; then
	[ -n "$MENHIR_TOOL" ] || {
		echo "ocaml_compile: --menhir-module given without --menhir-tool" >&2
		exit 2
	}
	SCRATCH="$(mktemp -d)"
	for s in "$WORK"/*.ml "$WORK"/*.mli; do [ -e "$s" ] && cp "$s" "$SCRATCH/"; done
	SIB="$(cd "$SCRATCH" && ls -- *.ml *.mli 2>/dev/null || true)"
	for f in $(cd "$SCRATCH" && "$OCAMLDEP" -sort $SIB 2>/dev/null || echo "$SIB"); do
		# Best effort: a sibling that needs the not-yet-generated parser fails
		# here and is simply absent from the inference context (it is not needed).
		"$OCAMLOPT" $CFLAGS -I "$SCRATCH" $INCFLAGS -c "$SCRATCH/$f" 2>/dev/null || true
	done
	for g in $MENHIR_MODULES; do
		GMLY="$WORK/$g.mly"
		[ -e "$GMLY" ] || {
			echo "ocaml_compile: menhir module $g has no $g.mly in srcs" >&2
			exit 2
		}
		"$MENHIR_TOOL" $MENHIR_FLAGS --infer-write-query "$SCRATCH/${g}__query.ml" "$GMLY"
		"$OCAMLOPT" $CFLAGS -I "$SCRATCH" $INCFLAGS -i "$SCRATCH/${g}__query.ml" >"$SCRATCH/${g}.inferred"
		"$MENHIR_TOOL" $MENHIR_FLAGS --infer-read-reply "$SCRATCH/${g}.inferred" --base "$WORK/$g" "$GMLY"
		rm -f "$GMLY"
	done
	rm -rf "$SCRATCH"
fi

# --- Recover compile order over the sources ---------------------------------
# Sort .ml AND .mli together: an interface may depend on a module whose
# implementation sorts late (re's category.mli references Fmt), so interface
# compile order is a property of the full file graph, not the .ml-only graph.
# ocamldep -sort emits one topological file order with x.mli before x.ml.
# The list comes from $WORK (not $SRCS) so generated sources are included.
ALLB="$(cd "$WORK" && ls -- *.ml *.mli 2>/dev/null || true)"
ORDER="$(cd "$WORK" && "$OCAMLDEP" -sort $ALLB)"
echo "ocaml_compile: compile order: $ORDER" >&2

# --- Wrapping (dune scheme) --------------------------------------------------
# Members become <lib>__<Module> behind a generated alias module; everything
# compiles with -open <alias>. Two cases, matching dune:
#   * the library provides a main module named exactly <lib>.ml: it becomes the
#     public module <Lib>, and the alias is the internal <Lib>__ (members open
#     that; the main module re-exports what it wants).
#   * no such main module: the alias module *is* the public wrapper <Lib>
#     (dune generates it), so consumers see <Lib>.<Member>.
# ocamldep ran on the original names above (members reference each other by
# plain name); the rename happens after sorting, preserving the sorted order.
capitalize() {
	_h=$(printf %.1s "$1" | tr '[:lower:]' '[:upper:]')
	printf '%s%s' "$_h" "${1#?}"
}

CMX_LIST=""
OPENFLAG=""
if [ "$WRAPPED" = "1" ]; then
	# The "main module" of library <lib> is the module named <Lib> (capitalized
	# lib name). Its source may be lib.ml OR Lib.ml -- both yield module <Lib> --
	# so the comparison is on module names (capitalized), not raw file names.
	NAME_MOD="$(capitalize "$NAME")"
	HAS_MAIN=0
	for f in $ORDER; do
		[ "$(capitalize "${f%.*}")" = "$NAME_MOD" ] && HAS_MAIN=1
	done
	if [ "$HAS_MAIN" = "1" ]; then
		ALIAS_MOD="${NAME}__"
	else
		# The alias module is the public wrapper itself.
		ALIAS_MOD="$NAME"
	fi
	ALIAS_ML="$WORK/$ALIAS_MOD.ml"
	: >"$ALIAS_ML"
	SEEN=" "
	RENAMED_ORDER=""
	for f in $ORDER; do
		base="${f%.*}"
		ext="${f##*.}"
		if [ "$(capitalize "$base")" = "$NAME_MOD" ]; then
			RENAMED_ORDER="$RENAMED_ORDER $f"
			continue
		fi
		Mod="$(capitalize "$base")"
		mv "$WORK/$f" "$WORK/${NAME}__$Mod.$ext"
		# One alias line per module (a unit's .mli and .ml both pass here).
		case "$SEEN" in
		*" $base "*) ;;
		*)
			echo "module $Mod = $(capitalize "${NAME}__$Mod")" >>"$ALIAS_ML"
			SEEN="$SEEN$base "
			;;
		esac
		RENAMED_ORDER="$RENAMED_ORDER ${NAME}__$Mod.$ext"
	done
	ORDER="$RENAMED_ORDER"
	# -no-alias-deps: the alias module references member cmis that do not exist
	# yet; -w -49 silences the missing-cmi warning that would otherwise error.
	"$OCAMLOPT" $CFLAGS $INCFLAGS -no-alias-deps -w -49 -c "$ALIAS_ML"
	CMX_LIST="$WORK/$ALIAS_MOD.cmx"
	OPENFLAG="-open $(capitalize "$ALIAS_MOD")"
fi

# --- Compile each file in sorted order ---------------------------------------
# -no-alias-deps is harmless when unwrapped (OPENFLAG empty, no alias module).
for f in $ORDER; do
	"$OCAMLOPT" $CFLAGS $INCFLAGS $OPENFLAG -no-alias-deps -c "$WORK/$f"
	case "$f" in
	*.ml) CMX_LIST="$CMX_LIST $WORK/${f%.ml}.cmx" ;;
	esac
done

# --- Compile C stub sources (if any) ----------------------------------------
# ocamlopt compiles .c directly (it supplies caml/*.h) using the execution
# host's C compiler; the .o lands next to the source in $WORK.
STUB_OBJS=""
for c in $CSRCS; do
	cb="$(basename "$c")"
	cp "$c" "$WORK/$cb"
	# -ccopt -I<dir> lets a stub #include a cc_deps header (pcre2.h etc.).
	(cd "$WORK" && "$OCAMLOPT" $CCOPT_INC -c "$cb")
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
	# any C stub objects compiled for this binary directly. -linkall keeps
	# units nothing references: ppx rewriters register themselves with ppxlib
	# at module init, so a driver's rewriters are exactly such units. The
	# external C archives (cc_deps) and their link flags come last, after the
	# stub objects that reference their symbols (static link resolves L-to-R).
	LINKFLAGS=""
	[ "$LINKALL" = "1" ] && LINKFLAGS="-linkall"
	"$OCAMLOPT" $CFLAGS $LINKFLAGS $INCFLAGS $PKG_LINK $CMXAS $CMX_LIST $STUB_OBJS $CC_ARCH_ABS $CC_CCLIB -o "$EXE_OUT"
fi
