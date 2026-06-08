#!/bin/sh
# Whole-library OCaml compile/link driver, run inside the pinned OCaml container.
#
# Recovers compile order with `ocamldep -sort`, compiles each module's .mli
# before its .ml, then either archives the .cmx into a .cmxa (library mode) or
# links a native executable (binary mode). See bazel/ocaml/rules.bzl for why this
# is one whole-library action rather than per-module.
set -eu

MODE="" NAME="" OPAM_ROOT="" USE_FIND="1"
INCLUDES="" OPAM_PKGS="" SRCS="" CMXAS="" CFLAGS=""
OBJS_OUT="" CMXA_OUT="" A_OUT="" EXE_OUT=""

while [ $# -gt 0 ]; do
	case "$1" in
	--mode) MODE="$2" && shift 2 ;;
	--name) NAME="$2" && shift 2 ;;
	--opam-root) OPAM_ROOT="$2" && shift 2 ;;
	--use-ocamlfind) USE_FIND="$2" && shift 2 ;;
	--compile-flag) CFLAGS="$CFLAGS $2" && shift 2 ;;
	--include) INCLUDES="$INCLUDES $2" && shift 2 ;;
	--opam-pkg) OPAM_PKGS="$OPAM_PKGS $2" && shift 2 ;;
	--src) SRCS="$SRCS $2" && shift 2 ;;
	--cmxa) CMXAS="$CMXAS $2" && shift 2 ;;
	--objs-out) OBJS_OUT="$2" && shift 2 ;;
	--cmxa-out) CMXA_OUT="$2" && shift 2 ;;
	--a-out) A_OUT="$2" && shift 2 ;;
	--exe-out) EXE_OUT="$2" && shift 2 ;;
	*) echo "ocaml_compile: unknown arg: $1" >&2 && exit 2 ;;
	esac
done

# --- Bring the opam switch toolchain onto PATH inside the container ----------
export OPAMROOTISOK=1 OPAMYES=1
if [ -n "$OPAM_ROOT" ]; then
	export OPAMROOT="$OPAM_ROOT"
	for d in "$OPAMROOT"/default/bin "$OPAMROOT"/*/bin; do
		[ -d "$d" ] && PATH="$d:$PATH"
	done
	export PATH
	if command -v opam >/dev/null 2>&1; then
		eval "$(opam env --root="$OPAMROOT" --set-root 2>/dev/null)" || true
	fi
fi

# Diagnostic probe: proves in the action log which image actually ran and
# whether the opam switch is present (i.e. whether container-image was honored).
echo "ocaml_compile: probe os=$(. /etc/os-release 2>/dev/null && echo "${ID:-?}-${VERSION_ID:-?}") opamroot=$OPAM_ROOT exists=$([ -d "$OPAM_ROOT" ] && echo yes || echo no) user=$(id -un 2>/dev/null || echo '?')" >&2

# --- Resolve tools. Prefer ocamlfind; fall back to raw compiler + stdlib map --
OCAMLOPT="ocamlopt"
FIND=""
if [ "$USE_FIND" = "1" ] && command -v ocamlfind >/dev/null 2>&1; then
	FIND="ocamlfind"
	OCAMLOPT="ocamlfind ocamlopt"
fi
command -v ocamlopt >/dev/null 2>&1 || {
	echo "ocaml_compile: ocamlopt not found on PATH ($PATH)" >&2
	exit 3
}
echo "ocaml_compile: mode=$MODE find=${FIND:-none} ocamlopt=$(command -v ocamlopt)" >&2

# --- Build include + opam package flags -------------------------------------
INCFLAGS=""
for d in $INCLUDES; do INCFLAGS="$INCFLAGS -I $d"; done

PKG_COMMA=""
for p in $OPAM_PKGS; do
	if [ -z "$PKG_COMMA" ]; then PKG_COMMA="$p"; else PKG_COMMA="$PKG_COMMA,$p"; fi
done
PKG_COMPILE="" PKG_LINK=""
if [ -n "$FIND" ]; then
	if [ -n "$PKG_COMMA" ]; then
		PKG_COMPILE="-package $PKG_COMMA"
		PKG_LINK="-package $PKG_COMMA -linkpkg"
	fi
else
	# No findlib: resolve stdlib-shipped packages via `-I +pkg pkg.cmxa`.
	for p in $OPAM_PKGS; do
		PKG_COMPILE="$PKG_COMPILE -I +$p"
		PKG_LINK="$PKG_LINK -I +$p $p.cmxa"
	done
fi

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
ORDER="$(cd "$WORK" && ocamldep -sort $MLB)"
echo "ocaml_compile: compile order: $ORDER" >&2

# --- Compile each module (.mli before .ml) ----------------------------------
CMX_LIST=""
for ml in $ORDER; do
	base="${ml%.ml}"
	if [ -f "$WORK/$base.mli" ]; then
		$OCAMLOPT $CFLAGS $PKG_COMPILE $INCFLAGS -c "$WORK/$base.mli"
	fi
	$OCAMLOPT $CFLAGS $PKG_COMPILE $INCFLAGS -c "$WORK/$ml"
	CMX_LIST="$CMX_LIST $WORK/$base.cmx"
done

# --- Produce the output -----------------------------------------------------
if [ "$MODE" = "library" ]; then
	# ocamlopt -o NAME.cmxa also writes NAME.a alongside it.
	$OCAMLOPT -a -o "$CMXA_OUT" $CMX_LIST
	[ "$A_OUT" = "${CMXA_OUT%.cmxa}.a" ] || cp "${CMXA_OUT%.cmxa}.a" "$A_OUT"
else
	# Link order: stdlib/opam archives, then deps (postorder), then own modules.
	$OCAMLOPT $CFLAGS $PKG_LINK $INCFLAGS $CMXAS $CMX_LIST -o "$EXE_OUT"
fi
