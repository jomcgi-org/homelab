"""Thin native OCaml rules: ocaml_library and ocaml_binary.

Design (a deliberate toy, see bazel/ocaml/README.md):

  * One whole-library compile action. The driver runs `ocamldep -sort` over the
    library's sources to recover compile order, compiles each module's .mli
    before its .ml, then archives the .cmx in order into a .cmxa. This trades
    fine-grained per-module incrementality for simplicity — fine for a toy. The
    "real" version is a Gazelle/ocamldep BUILD generator emitting one target per
    module (called out as the next step in the README).

  * Compilation runs inside a digest-pinned OCaml container on RBE
    (toolchain.bzl EXEC_PROPERTIES); the rules themselves ship no compiler.

  * OcamlInfo carries the compiled output dir (cmi/cmx/.o), the .cmxa archive +
    its .a, transitive include dirs, and transitive opam (findlib) package names
    in link order.
"""

load(":toolchain.bzl", "EXEC_PROPERTIES")

OcamlInfo = provider(
    doc = "Outputs and transitive link information for an OCaml library.",
    fields = {
        "objs_dir": "Directory artifact with this library's .cmi/.cmx/.o (and copied sources).",
        "cmxa": "The native archive (.cmxa) for this library.",
        "a_lib": "The C object archive (.a) paired with the .cmxa.",
        "transitive_includes": "depset of objs_dir artifacts for self + all deps (compile -I path).",
        "transitive_cmxa": "depset (postorder: deps before dependents) of .cmxa for link order.",
        "transitive_a": "depset of .a artifacts that must be staged when linking.",
        "transitive_opam": "depset of findlib/opam package names required transitively.",
    },
)

_TOOLCHAIN_TYPE = "//bazel/ocaml:toolchain_type"

def _collect(ctx):
    """Gather transitive include dirs, cmxa, .a and opam packages from deps."""
    inc = []
    cmxa = []
    a = []
    opam = [depset(ctx.attr.opam_deps)]
    for dep in ctx.attr.deps:
        info = dep[OcamlInfo]
        inc.append(info.transitive_includes)
        cmxa.append(info.transitive_cmxa)
        a.append(info.transitive_a)
        opam.append(info.transitive_opam)
    return struct(
        includes = depset(transitive = inc),
        cmxa = depset(transitive = cmxa, order = "postorder"),
        a = depset(transitive = a),
        opam = depset(transitive = opam),
    )

def _driver_args(ctx, tc, mode, include_dirs, opam_pkgs, srcs):
    args = ctx.actions.args()
    args.add("--mode", mode)
    args.add("--name", ctx.label.name)
    args.add("--opam-root", tc.opam_root)
    args.add("--use-ocamlfind", "1" if tc.use_ocamlfind else "0")
    for f in tc.extra_compile_flags:
        args.add("--compile-flag", f)
    for d in include_dirs:
        args.add("--include", d.path)
    for p in opam_pkgs:
        args.add("--opam-pkg", p)
    for s in srcs:
        args.add("--src", s.path)
    return args

def _ocaml_library_impl(ctx):
    tc = ctx.toolchains[_TOOLCHAIN_TYPE].ocaml
    dep = _collect(ctx)

    objs_dir = ctx.actions.declare_directory(ctx.label.name + "_objs")
    cmxa = ctx.actions.declare_file(ctx.label.name + ".cmxa")
    a_lib = ctx.actions.declare_file(ctx.label.name + ".a")

    include_dirs = dep.includes.to_list()
    opam_pkgs = dep.opam.to_list()

    args = _driver_args(ctx, tc, "library", include_dirs, opam_pkgs, ctx.files.srcs)
    args.add("--objs-out", objs_dir.path)
    args.add("--cmxa-out", cmxa.path)
    args.add("--a-out", a_lib.path)

    ctx.actions.run(
        executable = ctx.executable._driver,
        arguments = [args],
        inputs = depset(ctx.files.srcs, transitive = [dep.includes, dep.cmxa, dep.a]),
        outputs = [objs_dir, cmxa, a_lib],
        mnemonic = "OcamlLibrary",
        progress_message = "Compiling OCaml library %{label}",
    )

    info = OcamlInfo(
        objs_dir = objs_dir,
        cmxa = cmxa,
        a_lib = a_lib,
        transitive_includes = depset([objs_dir], transitive = [dep.includes]),
        transitive_cmxa = depset([cmxa], transitive = [dep.cmxa], order = "postorder"),
        transitive_a = depset([a_lib], transitive = [dep.a]),
        transitive_opam = depset(ctx.attr.opam_deps, transitive = [dep.opam]),
    )
    return [
        info,
        DefaultInfo(files = depset([cmxa, a_lib, objs_dir])),
    ]

def _ocaml_binary_impl(ctx):
    tc = ctx.toolchains[_TOOLCHAIN_TYPE].ocaml
    dep = _collect(ctx)

    exe = ctx.actions.declare_file(ctx.label.name)

    include_dirs = dep.includes.to_list()
    cmxa_list = dep.cmxa.to_list()  # postorder: dependencies before dependents
    opam_pkgs = dep.opam.to_list()

    args = _driver_args(ctx, tc, "binary", include_dirs, opam_pkgs, ctx.files.srcs)
    args.add("--exe-out", exe.path)
    for c in cmxa_list:
        args.add("--cmxa", c.path)

    ctx.actions.run(
        executable = ctx.executable._driver,
        arguments = [args],
        inputs = depset(ctx.files.srcs, transitive = [dep.includes, dep.cmxa, dep.a]),
        outputs = [exe],
        mnemonic = "OcamlBinary",
        progress_message = "Linking OCaml binary %{label}",
    )

    return [DefaultInfo(
        files = depset([exe]),
        executable = exe,
        runfiles = ctx.runfiles(files = [exe]),
    )]

_COMMON_ATTRS = {
    "srcs": attr.label_list(
        allow_files = [".ml", ".mli"],
        mandatory = True,
        doc = ".ml/.mli sources; compile order is recovered automatically via ocamldep -sort.",
    ),
    "deps": attr.label_list(
        providers = [OcamlInfo],
        doc = "Other ocaml_library targets.",
    ),
    "opam_deps": attr.string_list(
        doc = "findlib/opam package names (e.g. \"unix\", \"str\"). Resolved via ocamlfind when available.",
    ),
    "_driver": attr.label(
        default = "//bazel/ocaml/driver:ocaml_compile",
        executable = True,
        cfg = "exec",
    ),
}

_ocaml_library = rule(
    implementation = _ocaml_library_impl,
    attrs = _COMMON_ATTRS,
    toolchains = [_TOOLCHAIN_TYPE],
)

_ocaml_binary = rule(
    implementation = _ocaml_binary_impl,
    attrs = _COMMON_ATTRS,
    toolchains = [_TOOLCHAIN_TYPE],
    executable = True,
)

def ocaml_library(name, srcs, deps = [], opam_deps = [], **kwargs):
    """Compile a set of .ml/.mli modules into a native .cmxa archive.

    The macro attaches the OCaml container image (toolchain.bzl EXEC_PROPERTIES)
    so the compile action runs on RBE in an environment with ocamlopt on PATH.
    """
    _ocaml_library(
        name = name,
        srcs = srcs,
        deps = deps,
        opam_deps = opam_deps,
        exec_properties = EXEC_PROPERTIES,
        **kwargs
    )

def ocaml_binary(name, srcs, deps = [], opam_deps = [], **kwargs):
    """Compile + link a runnable native OCaml executable."""
    _ocaml_binary(
        name = name,
        srcs = srcs,
        deps = deps,
        opam_deps = opam_deps,
        exec_properties = EXEC_PROPERTIES,
        **kwargs
    )
