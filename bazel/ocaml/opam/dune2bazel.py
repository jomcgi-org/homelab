#!/usr/bin/env python3
"""Translate a package's Dune `(library ...)` stanzas into a Bazel BUILD.

Run by the @ocaml_* repository rules (bazel/ocaml/opam/extension.bzl) via the
host python3 during repo fetch -- NOT a Bazel py_binary. It reads the fetched
opam package's real `dune` files and emits a BUILD that drives our //bazel/ocaml
rules, so the dependency builds from its own dune metadata instead of a
hand-transcribed target. Opam packages are themselves dune projects, so
translating their dune files *is* resolving the dependency from source.

Modeled today: multiple `(library)` stanzas across multiple dune dirs;
`wrapped` (dune defaults to wrapped); `(libraries ...)` resolving to stdlib
archives, compiler-libs sublibraries, `re_export`, and other locked packages
via the lib_map; `(preprocess no_preprocessing)` and `(preprocess (pps ...))`
(the latter generates an ocaml_ppx driver target); `(kind ppx_rewriter |
ppx_deriver)`; `flags`/`ocamlopt_flags` (minus :standard).

Anything else that changes build semantics -- module filtering, C stubs,
`(rule)` codegen, instrumentation -- is rejected loudly rather than silently
mishandled. That rejection is the precise marker of where translation work (or
an opam/overrides/ BUILD) has to begin.
"""

import argparse
import json
import sys

# findlib packages that ship with the OCaml stdlib. "seq" lives inside
# stdlib.cmxa (no separate archive, so no opam_dep needed); unix/str/threads &c
# are separate stdlib archives our rules link by name via opam_deps, and
# compiler-libs sublibraries resolve against the sysroot's +compiler-libs.
_STDLIB_NO_ARCHIVE = {"seq", "bytes", "result", "stdlib"}
_STDLIB_ARCHIVE = {"unix", "str", "threads", "dynlink", "bigarray"}
_COMPILER_LIBS = {
    "compiler-libs.common",
    "compiler-libs.bytecomp",
    "compiler-libs.optcomp",
}

_SUPPORTED_LIBRARY_FIELDS = {
    "name",
    "public_name",
    "synopsis",
    "libraries",
    "wrapped",
    "preprocess",
    "kind",
    "flags",
    "ocamlopt_flags",
    # Coverage instrumentation (e.g. (instrumentation (backend bisect_ppx)))
    # is a no-op unless dune is invoked with --instrument-with; we never
    # instrument, so the field is provably inert and safe to drop.
    "instrumentation",
}

# Non-library stanzas that provably do not affect how the library compiles.
_INERT_STANZAS = {
    "documentation",
    "env",
    "dirs",
    "data_only_dirs",
    "vendored_dirs",
    "ocamlformat",
}

_SUPPORTED_KINDS = {"normal", "ppx_rewriter", "ppx_deriver"}


def tokenize(text):
    """Tokenize Dune S-expressions: '(', ')', ('atom'|'str', value)."""
    toks = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == ";":  # line comment
            while i < n and text[i] != "\n":
                i += 1
        elif c in "()":
            toks.append(c)
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            toks.append(("str", "".join(buf)))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '();"':
                j += 1
            toks.append(("atom", text[i:j]))
            i = j
    return toks


def parse(toks):
    """Parse a flat token stream into a list of top-level S-expressions."""
    pos = [0]

    def expr():
        t = toks[pos[0]]
        if t == "(":
            pos[0] += 1
            lst = []
            while toks[pos[0]] != ")":
                lst.append(expr())
            pos[0] += 1  # consume ')'
            return lst
        if t == ")":
            sys.exit("dune2bazel: unbalanced ')' in dune file")
        pos[0] += 1
        return t  # ('atom'|'str', value)

    out = []
    while pos[0] < len(toks):
        out.append(expr())
    return out


def _atom(x):
    if isinstance(x, tuple):
        return x[1]
    sys.exit("dune2bazel: expected an atom, got a list: %r" % (x,))


def _field(stanza, key):
    """Return the tail of the first `(key ...)` sub-stanza, or None."""
    for item in stanza[1:]:
        if (
            isinstance(item, list)
            and item
            and isinstance(item[0], tuple)
            and item[0][1] == key
        ):
            return item[1:]
    return None


def _resolve_libraries(libs, lib_map):
    """Map dune (libraries ...) names to (opam_deps, dep labels)."""
    opam_deps = []
    deps = []
    for lib in libs:
        if isinstance(lib, list):
            # (re_export x) re-exports x to the consumer's consumers; our
            # transitive provider model already propagates everything, so it
            # collapses to a plain dependency.
            if lib and isinstance(lib[0], tuple) and lib[0][1] == "re_export":
                sub_opam, sub_deps = _resolve_libraries(lib[1:], lib_map)
                opam_deps += sub_opam
                deps += sub_deps
                continue
            sys.exit(
                "dune2bazel: unsupported (libraries ...) form %r (select/clause "
                "expressions are not modeled)." % (lib,)
            )
        pkg = _atom(lib)
        if pkg in _STDLIB_NO_ARCHIVE:
            continue
        if pkg in _STDLIB_ARCHIVE or pkg in _COMPILER_LIBS:
            opam_deps.append(pkg)
            continue
        if pkg in lib_map:
            deps.append(lib_map[pkg])
            continue
        sys.exit(
            "dune2bazel: (libraries %s) is neither a stdlib/compiler-libs "
            "package nor a library declared in bazel/ocaml/opam/lock.json "
            "(libs tables). Add the providing package to the lock, or extend "
            "its libs map." % pkg
        )
    return opam_deps, deps


def _resolve_flags(stanza):
    """Collect (flags ...) + (ocamlopt_flags ...), dropping :standard."""
    flags = []
    for key in ("flags", "ocamlopt_flags"):
        field = _field(stanza, key)
        if field is None:
            continue
        for item in field:
            if isinstance(item, list):
                # (:standard extra...) is additive, same as the flat form.
                # Subtraction (\) and other set operators are not modeled.
                for x in item:
                    if isinstance(x, list) or _atom(x) == "\\":
                        sys.exit(
                            "dune2bazel: unsupported (%s ...) form %r (only "
                            "plain flags and additive :standard are modeled)."
                            % (key, item)
                        )
                    if _atom(x) != ":standard":
                        flags.append(_atom(x))
                continue
            flag = _atom(item)
            if flag == ":standard":
                continue
            flags.append(flag)
    return flags


def _resolve_preprocess(stanza, name, lib_map):
    """Translate (preprocess ...). Returns (ppx_target_lines, preprocess_attr)."""
    field = _field(stanza, "preprocess")
    if field is None:
        return [], None
    if (
        len(field) == 1
        and isinstance(field[0], tuple)
        and field[0][1] == "no_preprocessing"
    ):
        return [], None
    if (
        len(field) == 1
        and isinstance(field[0], list)
        and field[0]
        and isinstance(field[0][0], tuple)
        and field[0][0][1] == "pps"
    ):
        rewriters = []
        for item in field[0][1:]:
            pps_name = _atom(item)
            if pps_name.startswith("-"):
                sys.exit(
                    "dune2bazel: (pps ... %s) rewriter arguments are not "
                    "modeled yet." % pps_name
                )
            if pps_name not in lib_map:
                sys.exit(
                    "dune2bazel: (pps %s) does not name a library declared in "
                    "bazel/ocaml/opam/lock.json (libs tables)." % pps_name
                )
            rewriters.append(lib_map[pps_name])
        ppx_name = name + "_ppx"
        lines = [
            "",
            "# ppx driver for (preprocess (pps ...)); rewriters register with",
            "# ppxlib at module-init, so linking them composes the driver.",
            "ocaml_ppx(",
            '    name = "%s",' % ppx_name,
            "    deps = [",
        ]
        lines += ['        "%s",' % r for r in rewriters]
        lines += ["    ],", ")"]
        return lines, ":" + ppx_name
    sys.exit(
        "dune2bazel: unsupported (preprocess ...) form %r. Modeled: "
        "no_preprocessing and (pps ...); per_module/action forms need an "
        "opam/overrides/ BUILD." % (field,)
    )


def _parse_menhir(stanza):
    """Translate a (menhir (modules ...) (flags ...)) stanza."""
    modules_field = _field(stanza, "modules")
    if not modules_field:
        sys.exit("dune2bazel: (menhir ...) without (modules ...)")
    modules = [_atom(m) for m in modules_field]
    flags = []
    flags_field = _field(stanza, "flags")
    if flags_field:
        for item in flags_field:
            if isinstance(item, list):
                sys.exit(
                    "dune2bazel: unsupported (menhir (flags ...)) form %r" % (item,)
                )
            flags.append(_atom(item))
    return modules, flags


def gen_library(stanza, src_dir, lib_map, menhir_modules=None, menhir_flags=None):
    for item in stanza[1:]:
        if not (isinstance(item, list) and item and isinstance(item[0], tuple)):
            sys.exit("dune2bazel: unexpected item in (library): %r" % (item,))
        key = item[0][1]
        if key not in _SUPPORTED_LIBRARY_FIELDS:
            sys.exit(
                "dune2bazel: unsupported (library) field %r. This field implies "
                "a dune feature (C stubs, module filtering, codegen, ...) we do "
                "not model yet; extend the translator or add an "
                "opam/overrides/ BUILD." % key
            )

    # Dune derives the internal name from public_name when (name ...) is
    # omitted; that is only valid when the public name has no dots.
    name_field = _field(stanza, "name") or _field(stanza, "public_name")
    if not name_field:
        sys.exit("dune2bazel: (library) has neither (name ...) nor (public_name ...)")
    name = _atom(name_field[0])
    if "." in name:
        sys.exit(
            "dune2bazel: (library) has no (name ...) and public_name %r is "
            "dotted; dune would reject this too." % name
        )

    kind_field = _field(stanza, "kind")
    if kind_field:
        kind = _atom(kind_field[0])
        if kind not in _SUPPORTED_KINDS:
            sys.exit("dune2bazel: unsupported (kind %s)" % kind)

    # Dune's default is wrapped; only an explicit (wrapped false) opts out.
    # Wrapping namespaces member modules as <Lib>__<Module>, which is what
    # keeps third-party internal module names (re's Fmt/Str) from colliding.
    wrapped_field = _field(stanza, "wrapped")
    wrapped = True
    if wrapped_field and _atom(wrapped_field[0]) == "false":
        wrapped = False

    opam_deps, deps = _resolve_libraries(_field(stanza, "libraries") or [], lib_map)
    flags = _resolve_flags(stanza)
    ppx_lines, preprocess = _resolve_preprocess(stanza, name, lib_map)

    lines = list(ppx_lines)
    lines += [
        "",
        "ocaml_library(",
        '    name = "%s",' % name,
        '    srcs = glob(["%s/*.ml", "%s/*.mli", "%s/*.mll", "%s/*.mly"], allow_empty = True),'
        % (src_dir, src_dir, src_dir, src_dir),
    ]
    if wrapped:
        lines.append("    wrapped = True,")
    if preprocess:
        lines.append('    preprocess = "%s",' % preprocess)
    if flags:
        lines.append(
            "    ocamlopt_flags = [%s]," % ", ".join('"%s"' % f for f in flags)
        )
    if menhir_modules:
        lines.append(
            "    menhir = [%s]," % ", ".join('"%s"' % m for m in menhir_modules)
        )
        if menhir_flags:
            lines.append(
                "    menhir_flags = [%s]," % ", ".join('"%s"' % f for f in menhir_flags)
            )
        lines.append('    menhir_tool = "@ocaml_menhir//:menhir",')
    if deps:
        lines.append("    deps = [%s]," % ", ".join('"%s"' % d for d in sorted(deps)))
    if opam_deps:
        lines.append("    opam_deps = [%s]," % ", ".join('"%s"' % d for d in opam_deps))
    lines += [
        '    visibility = ["//visibility:public"],',
        ")",
    ]
    return "\n".join(lines) + "\n"


def gen_dune_dir(dune_path, src_dir, lib_map):
    """Translate every (library) stanza of one dune file; returns target text."""
    with open(dune_path, "r") as f:
        stanzas = parse(tokenize(f.read()))

    # ocamllex/menhir stanzas live alongside the library in the same dune file.
    # ocamllex modules are picked up by the library's *.mll glob, so the stanza
    # is informational. menhir modules attach to the library as a `menhir` attr.
    libraries = []
    menhir_modules = []
    menhir_flags = []
    for s in stanzas:
        if not (isinstance(s, list) and s and isinstance(s[0], tuple)):
            sys.exit("dune2bazel: unexpected top-level item: %r" % (s,))
        head = _atom(s[0])
        if head == "library":
            libraries.append(s)
        elif head == "ocamllex":
            continue  # the .mll is globbed into the library's srcs
        elif head == "menhir":
            mods, flags = _parse_menhir(s)
            menhir_modules += mods
            menhir_flags += flags
        elif head in _INERT_STANZAS:
            continue
        else:
            sys.exit(
                "dune2bazel: unsupported stanza (%s ...) in %s. Generic (rule) "
                "codegen and executable stanzas are not translated; packages "
                "that need them get an opam/overrides/ BUILD." % (head, dune_path)
            )
    if not libraries:
        sys.exit("dune2bazel: no (library) stanza in %s" % dune_path)
    if menhir_modules and len(libraries) != 1:
        sys.exit(
            "dune2bazel: a (menhir ...) stanza needs exactly one (library) in "
            "%s to attach to, found %d." % (dune_path, len(libraries))
        )

    out = []
    for i, lib in enumerate(libraries):
        if menhir_modules and i == 0:
            out.append(gen_library(lib, src_dir, lib_map, menhir_modules, menhir_flags))
        else:
            out.append(gen_library(lib, src_dir, lib_map))
    return "".join(out)


_HEADER = """\
# GENERATED by bazel/ocaml/opam/dune2bazel.py from this package's own
# dune metadata -- do not edit. Re-fetch the repo to regenerate.
load("@%s//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_ppx")
"""


def main(argv):
    ap = argparse.ArgumentParser(prog="dune2bazel.py")
    ap.add_argument("--root", required=True, help="root Bazel module name")
    ap.add_argument("--lib-map-json", default="{}", help="findlib name -> Bazel label")
    ap.add_argument(
        "--dune-dir",
        action="append",
        required=True,
        help="package-relative dir containing a dune file (repeatable)",
    )
    args = ap.parse_args(argv[1:])

    lib_map = json.loads(args.lib_map_json)
    out = [_HEADER % args.root]
    for d in args.dune_dir:
        out.append(gen_dune_dir("%s/dune" % d, d, lib_map))
    sys.stdout.write("".join(out))


if __name__ == "__main__":
    main(sys.argv)
