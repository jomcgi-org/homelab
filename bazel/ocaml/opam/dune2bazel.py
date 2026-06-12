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
via the lib_map; `(preprocess no_preprocessing | future_syntax)` (the latter
is the identity on our 5.3 sysroot) and `(preprocess (pps ...))`
(which generates an ocaml_ppx driver target); `(kind ppx_rewriter |
ppx_deriver)`; `flags`/`ocamlopt_flags` (minus :standard);
`(include_subdirs unqualified)` (a recursive source glob -- the driver's
stage-by-basename is exactly the flat namespace); `(ocamlyacc ...)` /
`(ocamllex ...)` stanzas (the globbed .mly/.mll go through the driver's
generation pipeline); `modules_without_implementation` (inert: mli-only
modules compile naturally); `ppx_runtime_libraries` (validated on the
declaring library, materialized on (pps ...) consumers as
preprocess_runtime_deps via the lock's ppx_runtime tables -- dune's
propagation, reproduced data-driven); `(modules ...)` lists that provably
equal the directory's globbed module set (validate-and-drop: the BUILD glob
already takes the whole dir, so a complete list is inert; an incomplete
list means real module filtering and rejects loudly); and the atdgen
codegen rule pair
`(rule (targets X_t.ml X_t.mli) (deps X.atd) (action (run atdgen ... %{deps})))`
(each rule becomes a genrule over the locked from-source atdgen, the shape
the ocaml-tree-sitter-core override hand-writes; the generated sources join
the library's srcs).

Anything else that changes build semantics -- module filtering, C stubs,
generic `(rule)` codegen, instrumentation -- is rejected loudly rather than
silently mishandled. That rejection is the precise marker of where translation
work (or an opam/overrides/ BUILD) has to begin.
"""

import argparse
import json
import os
import re
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
    # (modules ...) filters which sources join the library. Filtering itself
    # is not modeled (the BUILD glob takes the whole dir), but a list that
    # provably equals the dir's module set is inert: validated against the
    # filesystem in _validate_modules, then dropped. Anything else rejects.
    "modules",
    # Coverage instrumentation (e.g. (instrumentation (backend bisect_ppx)))
    # is a no-op unless dune is invoked with --instrument-with; we never
    # instrument, so the field is provably inert and safe to drop.
    "instrumentation",
    # Declares which mli-only modules are intentional (dune's lint against
    # forgotten .ml files). The driver compiles interface-only modules
    # naturally (ocamldep -sort includes them; only .cmx are archived), so
    # the declaration is provably inert.
    "modules_without_implementation",
    # (lint (pps ...)) only runs under `dune lint`, which we never invoke;
    # provably inert, same footing as instrumentation (sexplib/parsexp carry
    # it).
    "lint",
    # Names the libraries that code REWRITTEN by this ppx links at run time.
    # Our model keeps that on the consumer (the preprocess_runtime_deps attr,
    # the convention established with ppx_deriving.runtime), so the field
    # only validates here: every name must resolve against the lock, which
    # catches a missing runtime package at fetch time instead of at the
    # first consumer's link.
    "ppx_runtime_libraries",
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


def _module_name(stem):
    """Module name of a source stem (OCaml capitalizes the first letter)."""
    return stem[:1].upper() + stem[1:]


def _validate_modules(field, fs_dir, recursive, extra_srcs, dune_path):
    """Validate-and-drop a complete (modules ...) list.

    dune's (modules ...) selects which of the directory's sources belong to
    the library. Module filtering is not modeled (the emitted BUILD globs the
    whole dir), but when the listed set provably equals the globbed module
    set the field selects everything and is inert. The comparison runs over
    module names (source stems with the first letter capitalized, dune's own
    normalization): .ml/.mli/.mll/.mly stems count -- so mli-only modules
    do -- plus sources generated by translated (rule ...) stanzas. Any
    mismatch in either direction is real filtering and rejects loudly.
    """
    listed = set()
    for item in field:
        if isinstance(item, list):
            sys.exit(
                "dune2bazel: unsupported (modules ...) form %r in %s (set "
                "operations are not modeled; only a plain, complete module "
                "list validates and drops)." % (item, dune_path)
            )
        name = _atom(item)
        if name.startswith(":") or name == "\\":
            sys.exit(
                "dune2bazel: unsupported (modules ...) entry %r in %s (set "
                "operations are not modeled; only a plain, complete module "
                "list validates and drops)." % (name, dune_path)
            )
        listed.add(_module_name(name))

    exts = (".ml", ".mli", ".mll", ".mly")
    stems = set()
    if recursive:
        for root, _dirs, files in os.walk(fs_dir):
            for f in files:
                if f.endswith(exts):
                    stems.add(f.rsplit(".", 1)[0])
    else:
        for f in os.listdir(fs_dir):
            if f.endswith(exts) and os.path.isfile(os.path.join(fs_dir, f)):
                stems.add(f.rsplit(".", 1)[0])
    for f in extra_srcs or []:
        stems.add(f.rsplit(".", 1)[0])
    globbed = {_module_name(s) for s in stems}

    if listed == globbed:
        return
    unlisted = sorted(globbed - listed)
    phantom = sorted(listed - globbed)
    sys.exit(
        "dune2bazel: the (modules ...) list in %s does not equal the "
        "directory's module set; that is real module filtering, which is "
        "not modeled (only a complete list validates and drops). Modules "
        "in the dir but not listed: %s. Listed but with no source: %s. "
        "Filtered libraries need a source overlay or an opam/overrides/ "
        "BUILD." % (dune_path, unlisted or "none", phantom or "none")
    )


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


def _resolve_preprocess(stanza, name, lib_map, ppx_runtime_map):
    """Translate (preprocess ...).

    Returns (ppx_target_lines, preprocess_attr, runtime_dep_labels). The
    runtime labels reproduce dune's ppx_runtime_libraries propagation: code
    rewritten by (pps X) compiles against X's declared runtime libraries
    (ppx_compare emits references to Ppx_compare_lib), so the consumer gets
    them as preprocess_runtime_deps. The map comes from lock entries'
    `ppx_runtime` tables via the extension.
    """
    field = _field(stanza, "preprocess")
    if field is None:
        return [], None, []
    if (
        len(field) == 1
        and isinstance(field[0], tuple)
        and field[0][1] == "no_preprocessing"
    ):
        return [], None, []
    # future_syntax is dune's built-in shim that backports newer OCaml syntax
    # to older compilers; on a compiler at least as new as the syntax it
    # models it is the identity transform. The pinned sysroot is OCaml 5.3,
    # newer than everything future_syntax covers, so it drops to a no-op.
    if (
        len(field) == 1
        and isinstance(field[0], tuple)
        and field[0][1] == "future_syntax"
    ):
        return [], None, []
    if (
        len(field) == 1
        and isinstance(field[0], list)
        and field[0]
        and isinstance(field[0][0], tuple)
        and field[0][0][1] == "pps"
    ):
        rewriters = []
        runtime = []
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
            for label in ppx_runtime_map.get(pps_name, []):
                if label not in runtime:
                    runtime.append(label)
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
        return lines, ":" + ppx_name, runtime
    sys.exit(
        "dune2bazel: unsupported (preprocess ...) form %r. Modeled: "
        "no_preprocessing and (pps ...); per_module/action forms need an "
        "opam/overrides/ BUILD." % (field,)
    )


# The atdgen binary built from the locked atd source (the same target the
# ocaml-tree-sitter-core override's hand-written genrules use). The rule
# translation below only accepts `(run atdgen ...)`, so this is the one tool.
_ATDGEN_TOOL = "@ocaml_atd//:atdgen"

_RULE_FIELDS = {"targets", "deps", "action"}


def _parse_atdgen_rule(stanza, src_dir, dune_path):
    """Translate dune's atdgen codegen rule into a genrule.

    Models exactly the rule shape Semgrep's dune files (and the
    ocaml-tree-sitter-core override) use:

        (rule
         (targets X_t.ml X_t.mli)
         (deps    X.atd)
         (action  (run atdgen <flags...> %{deps})))

    and its -j sibling. atdgen derives output names from the input basename,
    so each target must be `<atd stem>_*.ml{,i}`. Any other (rule ...) shape
    still rejects loudly. Returns (genrule_lines, generated_filenames).
    """
    for item in stanza[1:]:
        if not (isinstance(item, list) and item and isinstance(item[0], tuple)):
            sys.exit("dune2bazel: unexpected item in (rule): %r" % (item,))
        if item[0][1] not in _RULE_FIELDS:
            sys.exit(
                "dune2bazel: unsupported (rule) field %r in %s; only the "
                "atdgen targets/deps/action shape is modeled." % (item[0][1], dune_path)
            )

    targets_field = _field(stanza, "targets")
    deps_field = _field(stanza, "deps")
    action_field = _field(stanza, "action")
    if not targets_field or not deps_field or not action_field:
        sys.exit(
            "dune2bazel: (rule ...) in %s needs (targets ...), (deps ...) "
            "and (action ...); only the atdgen rule shape is modeled." % dune_path
        )

    if len(deps_field) != 1:
        sys.exit(
            "dune2bazel: (rule (deps ...)) in %s must name exactly one .atd "
            "file, got %r." % (dune_path, deps_field)
        )
    atd = _atom(deps_field[0])
    if not atd.endswith(".atd"):
        sys.exit(
            "dune2bazel: (rule (deps %s)) in %s is not an .atd file; only "
            "the atdgen rule shape is modeled." % (atd, dune_path)
        )
    atd_stem = atd[: -len(".atd")]

    targets = [_atom(t) for t in targets_field]
    for t in targets:
        if not (t.endswith(".ml") or t.endswith(".mli")):
            sys.exit(
                "dune2bazel: (rule (targets ... %s ...)) in %s is not an "
                ".ml/.mli target." % (t, dune_path)
            )
        if not t.startswith(atd_stem + "_"):
            sys.exit(
                "dune2bazel: (rule) target %s in %s does not derive from "
                "%s (atdgen names outputs from the input basename)."
                % (t, dune_path, atd)
            )

    if not (len(action_field) == 1 and isinstance(action_field[0], list)):
        sys.exit(
            "dune2bazel: unsupported (rule (action ...)) form %r in %s."
            % (action_field, dune_path)
        )
    run = action_field[0]
    words = [_atom(w) for w in run]
    if len(words) < 3 or words[0] != "run" or words[1] != "atdgen":
        sys.exit(
            "dune2bazel: (rule (action ...)) in %s must be (run atdgen ... "
            "%%{deps}); got %r." % (dune_path, words)
        )
    if words[-1] != "%{deps}":
        sys.exit(
            "dune2bazel: (run atdgen ...) in %s must end with %%{deps}; "
            "got %r." % (dune_path, words)
        )
    args = words[2:-1]
    for a in args:
        if "%{" in a:
            sys.exit(
                "dune2bazel: unsupported dune variable %r in (run atdgen ...) "
                "in %s; only literal flags and a trailing %%{deps} are "
                "modeled." % (a, dune_path)
            )

    # Every name and flag is embedded verbatim in a Starlark string holding a
    # shell command; anything outside this charset (spaces, quotes,
    # backslashes, shell metacharacters, which dune CAN carry via quoted
    # atoms) would need escaping for both layers, so reject it instead.
    for word in [atd] + targets + args:
        if not re.fullmatch(r"[A-Za-z0-9._=-]+", word):
            sys.exit(
                "dune2bazel: %r in the (rule ...) in %s contains characters "
                "outside [A-Za-z0-9._=-]; the genrule embedding does not "
                "model escaping them." % (word, dune_path)
            )

    name = "atdgen_" + targets[0].rsplit(".", 1)[0]
    atd_path = "%s/%s" % (src_dir, atd)
    cmd = (
        "W=$$(mktemp -d) && AG=$$(realpath $(location %s)) && " % _ATDGEN_TOOL
        + "cp $(location %s) $$W/%s && " % (atd_path, atd)
        + "(cd $$W && $$AG %s %s) && " % (" ".join(args), atd)
        + " && ".join("cp $$W/%s $(location %s)" % (t, t) for t in targets)
    )
    lines = [
        "",
        "# dune (rule (action (run atdgen ...))) over %s, translated to a" % atd_path,
        "# genrule on the locked from-source atdgen.",
        "genrule(",
        '    name = "%s",' % name,
        '    srcs = ["%s"],' % atd_path,
        "    outs = [",
    ]
    lines += ['        "%s",' % t for t in targets]
    lines += [
        "    ],",
        '    cmd = "%s",' % cmd.replace('"', '\\"'),
        '    tools = ["%s"],' % _ATDGEN_TOOL,
        ")",
    ]
    return lines, targets


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


def gen_library(
    stanza,
    src_dir,
    lib_map,
    menhir_modules=None,
    menhir_flags=None,
    recursive=False,
    ppx_runtime_map=None,
    extra_srcs=None,
    fs_dir=None,
):
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

    # Validate-and-drop: a (modules ...) list that equals the directory's
    # module set selects everything the glob takes anyway; anything less
    # rejects inside the helper. fs_dir is where the sources actually live
    # (src_dir is the BUILD-relative dir, identical except under test).
    modules_field = _field(stanza, "modules")
    if modules_field is not None:
        _validate_modules(
            modules_field,
            fs_dir if fs_dir is not None else src_dir,
            recursive,
            extra_srcs,
            "%s/dune" % src_dir,
        )

    opam_deps, deps = _resolve_libraries(_field(stanza, "libraries") or [], lib_map)

    # Validate-and-drop: runtime linkage stays on the consumer (see
    # _SUPPORTED_LIBRARY_FIELDS), but unresolvable names reject loudly here.
    _resolve_libraries(_field(stanza, "ppx_runtime_libraries") or [], lib_map)
    flags = _resolve_flags(stanza)
    ppx_lines, preprocess, runtime_deps = _resolve_preprocess(
        stanza, name, lib_map, ppx_runtime_map or {}
    )

    # (include_subdirs unqualified) pulls sources from subdirectories into the
    # same flat module namespace; the driver stages every source by basename,
    # which is exactly that semantic, so it maps onto a recursive glob.
    pat = "%s/**/*" % src_dir if recursive else "%s/*" % src_dir
    # Sources generated by translated (rule ...) stanzas (atdgen codegen)
    # join the glob, the same shape the ocaml-tree-sitter-core override
    # hand-writes.
    extra = ""
    if extra_srcs:
        extra = " + [%s]" % ", ".join('"%s"' % s for s in extra_srcs)
    lines = list(ppx_lines)
    lines += [
        "",
        "ocaml_library(",
        '    name = "%s",' % name,
        '    srcs = glob(["%s.ml", "%s.mli", "%s.mll", "%s.mly"], allow_empty = True)%s,'
        % (pat, pat, pat, pat, extra),
    ]
    if wrapped:
        lines.append("    wrapped = True,")
    if preprocess:
        lines.append('    preprocess = "%s",' % preprocess)
    if runtime_deps:
        lines.append(
            "    preprocess_runtime_deps = [%s],"
            % ", ".join('"%s"' % d for d in sorted(runtime_deps))
        )
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


def gen_dune_dir(dune_path, src_dir, lib_map, ppx_runtime_map=None):
    """Translate every (library) stanza of one dune file; returns target text."""
    with open(dune_path, "r") as f:
        stanzas = parse(tokenize(f.read()))

    # ocamllex/menhir stanzas live alongside the library in the same dune file.
    # ocamllex modules are picked up by the library's *.mll glob, so the stanza
    # is informational. menhir modules attach to the library as a `menhir` attr.
    libraries = []
    menhir_modules = []
    menhir_flags = []
    rule_lines = []
    generated_srcs = []
    recursive = False
    for s in stanzas:
        if not (isinstance(s, list) and s and isinstance(s[0], tuple)):
            sys.exit("dune2bazel: unexpected top-level item: %r" % (s,))
        head = _atom(s[0])
        if head == "library":
            libraries.append(s)
        elif head == "rule":
            lines, gen = _parse_atdgen_rule(s, src_dir, dune_path)
            rule_lines += lines
            generated_srcs += gen
        elif head in ("ocamllex", "ocamlyacc"):
            continue  # the .mll/.mly is globbed into the library's srcs
        elif head == "include_subdirs":
            mode = _atom(s[1])
            if mode != "unqualified":
                sys.exit(
                    "dune2bazel: unsupported (include_subdirs %s) in %s; only "
                    "unqualified (flat namespace, matching the driver's "
                    "stage-by-basename) is modeled." % (mode, dune_path)
                )
            recursive = True
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
    if generated_srcs and len(libraries) != 1:
        # Without (modules ...) filtering there is no way to know which
        # library the generated sources belong to.
        sys.exit(
            "dune2bazel: (rule ...) generated sources need exactly one "
            "(library) in %s to attach to, found %d." % (dune_path, len(libraries))
        )

    out = []
    if rule_lines:
        out.append("\n".join(rule_lines) + "\n")
    for i, lib in enumerate(libraries):
        # menhir modules attach to the (single, enforced above) first library.
        first = i == 0
        out.append(
            gen_library(
                lib,
                src_dir,
                lib_map,
                menhir_modules if first else None,
                menhir_flags if first else None,
                recursive=recursive,
                ppx_runtime_map=ppx_runtime_map,
                extra_srcs=generated_srcs or None,
                fs_dir=os.path.dirname(dune_path),
            )
        )
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
        "--ppx-runtime-map-json",
        default="{}",
        help="pps rewriter name -> list of runtime dep Bazel labels",
    )
    ap.add_argument(
        "--dune-dir",
        action="append",
        required=True,
        help="package-relative dir containing a dune file (repeatable)",
    )
    args = ap.parse_args(argv[1:])

    lib_map = json.loads(args.lib_map_json)
    ppx_runtime_map = json.loads(args.ppx_runtime_map_json)
    out = [_HEADER % args.root]
    for d in args.dune_dir:
        out.append(gen_dune_dir("%s/dune" % d, d, lib_map, ppx_runtime_map))
    sys.stdout.write("".join(out))


if __name__ == "__main__":
    main(sys.argv)
