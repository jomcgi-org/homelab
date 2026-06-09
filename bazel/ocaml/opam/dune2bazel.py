#!/usr/bin/env python3
"""Translate a package's own Dune `(library ...)` stanza into a Bazel BUILD.

Run by the @ocaml_* repository rules (bazel/ocaml/opam/extension.bzl) via the
host python3 during repo fetch -- NOT a Bazel py_binary. It reads the fetched
opam package's real `dune` file and emits a BUILD that drives our //bazel/ocaml
rules, so the dependency builds from its own dune metadata instead of a
hand-transcribed target. This is the "have Bazel handle dune semantics" step:
opam packages are themselves dune projects, so translating their dune file *is*
resolving the dependency from source.

Scope (deliberately a toy -- see bazel/ocaml/README.md): the single `(library)`
stanza fields that re 1.11.0 actually uses (name / public_name / synopsis /
libraries). Anything that changes build semantics we do not yet model -- module
filtering, ppx `preprocess`, `foreign_stubs`/C, more than one stanza -- is
rejected loudly rather than silently mishandled. That rejection is the precise
marker of where real opam resolution would have to begin.

Usage: dune2bazel.py <dune-file> <src-subdir> <root-module-name>
Emits the BUILD file contents to stdout.
"""

import sys

# findlib packages that ship with the OCaml stdlib. "seq" lives inside
# stdlib.cmxa (no separate archive, so no opam_dep needed); unix/str/threads &c
# are separate stdlib archives our rules link by name via opam_deps.
_STDLIB_NO_ARCHIVE = {"seq", "bytes", "result", "stdlib"}
_STDLIB_ARCHIVE = {"unix", "str", "threads", "dynlink", "bigarray"}

_SUPPORTED_LIBRARY_FIELDS = {"name", "public_name", "synopsis", "libraries"}


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


def _resolve_libraries(libs):
    """Map dune (libraries ...) names to ocaml_library opam_deps."""
    opam_deps = []
    for lib in libs:
        pkg = _atom(lib)
        if pkg in _STDLIB_NO_ARCHIVE:
            continue
        if pkg in _STDLIB_ARCHIVE:
            opam_deps.append(pkg)
            continue
        sys.exit(
            "dune2bazel: (libraries %s) is not a stdlib-shipped findlib package. "
            "Real opam dependency resolution is not yet implemented; only stdlib "
            "packages resolve today (see bazel/ocaml/README.md)." % pkg
        )
    return opam_deps


def gen_library(stanza, src_dir, root_module):
    for item in stanza[1:]:
        if not (isinstance(item, list) and item and isinstance(item[0], tuple)):
            sys.exit("dune2bazel: unexpected item in (library): %r" % (item,))
        key = item[0][1]
        if key not in _SUPPORTED_LIBRARY_FIELDS:
            sys.exit(
                "dune2bazel: unsupported (library) field %r. This toy translates "
                "only pure-OCaml libraries; %r implies a dune feature (ppx, C "
                "stubs, module filtering) we do not model yet." % (key, key)
            )

    name_field = _field(stanza, "name")
    if not name_field:
        sys.exit("dune2bazel: (library) has no (name ...)")
    name = _atom(name_field[0])

    opam_deps = _resolve_libraries(_field(stanza, "libraries") or [])

    lines = [
        "# GENERATED by bazel/ocaml/opam/dune2bazel.py from this package's own",
        "# dune file -- do not edit. Re-fetch the repo to regenerate.",
        'load("@%s//bazel/ocaml:defs.bzl", "ocaml_library")' % root_module,
        "",
        "ocaml_library(",
        '    name = "%s",' % name,
        '    srcs = glob(["%s/*.ml", "%s/*.mli"]),' % (src_dir, src_dir),
    ]
    if opam_deps:
        lines.append("    opam_deps = [%s]," % ", ".join('"%s"' % d for d in opam_deps))
    lines += [
        '    visibility = ["//visibility:public"],',
        ")",
        "",
    ]
    return "\n".join(lines)


def main(argv):
    if len(argv) != 4:
        sys.exit("usage: dune2bazel.py <dune-file> <src-subdir> <root-module-name>")
    dune_file, src_dir, root_module = argv[1], argv[2], argv[3]

    with open(dune_file, "r") as f:
        stanzas = parse(tokenize(f.read()))

    libraries = [
        s for s in stanzas if isinstance(s, list) and s and _atom(s[0]) == "library"
    ]
    if len(libraries) != 1:
        sys.exit(
            "dune2bazel: expected exactly one (library) stanza, found %d. Multiple "
            "or non-library stanzas are out of scope for this toy." % len(libraries)
        )

    sys.stdout.write(gen_library(libraries[0], src_dir, root_module))


if __name__ == "__main__":
    main(sys.argv)
