"""Thin native OCaml rules: ocaml_library and ocaml_binary.

Design (a deliberate toy, see bazel/ocaml/README.md):

  * One whole-library compile action. The driver runs `ocamldep -sort` over the
    library's sources to recover compile order, compiles each module's .mli
    before its .ml, then archives the .cmx in order into a native .cmxa. This
    trades fine-grained per-module incrementality for simplicity — fine for a
    toy. The "real" version is a Gazelle/ocamldep BUILD generator emitting one
    target per module (the next step, called out in the README).

  * The compiler is a hermetic sysroot (toolchain.bzl) staged as action inputs;
    native linking uses the execution host's gcc/as/ld.

  * OcamlInfo carries the compiled output dir (cmi/cmx/.o), the .cmxa archive +
    its .a, transitive include dirs, and transitive opam/findlib package names
    in link order.
"""

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
    """Gather transitive include dirs, cmxa, .a and opam packages from deps.

    preprocess_runtime_deps (libraries the rewriter's generated code needs,
    e.g. ppx_deriving.runtime) are merged into the same walk: they must be
    compiled against and linked exactly like ordinary deps.
    """
    inc = []
    cmxa = []
    a = []
    opam = [depset(ctx.attr.opam_deps)]
    for dep in ctx.attr.deps + getattr(ctx.attr, "preprocess_runtime_deps", []):
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

def _tool_files(ctx):
    """Extra executables (preprocessors) that must stage as action inputs."""
    files = []
    for attr in ("pp", "cppo", "preprocess"):
        tool = getattr(ctx.executable, attr, None)
        if tool:
            files.append(tool)
    return files

def _driver_args(ctx, tc, mode, include_dirs, opam_pkgs, srcs, c_srcs):
    args = ctx.actions.args()
    args.add("--mode", mode)
    args.add("--name", ctx.label.name)
    args.add("--sysroot-tar", tc.sysroot_tar.path)
    args.add("--use-ocamlfind", "1" if tc.use_ocamlfind else "0")

    # Wrapping is a library concept; binary/test rules have no `wrapped` attr
    # and always pass 0. getattr keeps this helper shared across the rules.
    args.add("--wrapped", "1" if getattr(ctx.attr, "wrapped", False) else "0")

    for f in getattr(ctx.attr, "ocamlopt_flags", []):
        args.add("--compile-flag", f)

    # Source-generation pipeline tools (all optional, exec-config executables).
    if getattr(ctx.attr, "pp", None):
        args.add("--pp-tool", ctx.executable.pp.path)
        for a in ctx.attr.pp_args:
            args.add("--pp-arg", a)
    if getattr(ctx.attr, "cppo", None):
        args.add("--cppo-tool", ctx.executable.cppo.path)
    if getattr(ctx.attr, "preprocess", None):
        args.add("--ppx", ctx.executable.preprocess.path)
    for f in tc.extra_compile_flags:
        args.add("--compile-flag", f)
    for d in include_dirs:
        args.add("--include", d.path)
    for p in opam_pkgs:
        args.add("--opam-pkg", p)
    for s in srcs:
        args.add("--src", s.path)
    for c in c_srcs:
        args.add("--c-src", c.path)
    return args

def _ocaml_library_impl(ctx):
    tc = ctx.toolchains[_TOOLCHAIN_TYPE].ocaml
    dep = _collect(ctx)

    objs_dir = ctx.actions.declare_directory(ctx.label.name + "_objs")
    cmxa = ctx.actions.declare_file(ctx.label.name + ".cmxa")
    a_lib = ctx.actions.declare_file(ctx.label.name + ".a")

    args = _driver_args(ctx, tc, "library", dep.includes.to_list(), dep.opam.to_list(), ctx.files.srcs, ctx.files.c_srcs)
    args.add("--objs-out", objs_dir.path)
    args.add("--cmxa-out", cmxa.path)
    args.add("--a-out", a_lib.path)

    ctx.actions.run(
        executable = ctx.executable._driver,
        arguments = [args],
        inputs = depset(ctx.files.srcs + ctx.files.c_srcs + _tool_files(ctx), transitive = [dep.includes, dep.cmxa, dep.a, tc.sysroot_files]),
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

    args = _driver_args(ctx, tc, "binary", dep.includes.to_list(), dep.opam.to_list(), ctx.files.srcs, ctx.files.c_srcs)
    args.add("--exe-out", exe.path)
    for c in dep.cmxa.to_list():  # postorder: dependencies before dependents
        args.add("--cmxa", c.path)

    ctx.actions.run(
        executable = ctx.executable._driver,
        arguments = [args],
        inputs = depset(ctx.files.srcs + ctx.files.c_srcs + _tool_files(ctx), transitive = [dep.includes, dep.cmxa, dep.a, tc.sysroot_files]),
        outputs = [exe],
        mnemonic = "OcamlBinary",
        progress_message = "Linking OCaml binary %{label}",
    )

    return [DefaultInfo(
        files = depset([exe]),
        executable = exe,
        runfiles = ctx.runfiles(files = [exe] + ctx.files.data),
    )]

_COMMON_ATTRS = {
    "srcs": attr.label_list(
        allow_files = [".ml", ".mli", ".mll", ".mly"],
        mandatory = True,
        doc = ".ml/.mli sources; .mll/.mly are run through ocamllex/ocamlyacc first. " +
              "Compile order is recovered automatically via ocamldep -sort.",
    ),
    "c_srcs": attr.label_list(
        allow_files = [".c"],
        doc = "C stub sources (dune `foreign_stubs`/`c_names`). Compiled with ocamlopt " +
              "(which supplies the caml/*.h headers) and folded into the library's .a, so " +
              "binaries that link this library pull in the stubs automatically.",
    ),
    "deps": attr.label_list(
        providers = [OcamlInfo],
        doc = "Other ocaml_library targets.",
    ),
    "opam_deps": attr.string_list(
        doc = "findlib/opam package names resolved against the sysroot: stdlib-shipped " +
              "archives (\"unix\", \"str\") and the compiler-libs sublibraries " +
              "(\"compiler-libs.common\", \"compiler-libs.bytecomp\", \"compiler-libs.optcomp\").",
    ),
    "ocamlopt_flags": attr.string_list(
        doc = "Extra ocamlopt flags for this target's compiles (dune `(flags ...)` / " +
              "`(ocamlopt_flags ...)`, minus :standard).",
    ),
    "pp": attr.label(
        executable = True,
        cfg = "exec",
        doc = "Per-file preprocessor run over every staged .ml/.mli, output on stdout " +
              "(dune `(preprocess (action (run tool args %{input-file})))`).",
    ),
    "pp_args": attr.string_list(
        doc = "Arguments passed to `pp` before the input file. %OCAML_VERSION% and " +
              "%OCAML_AST_VERSION% are substituted by the driver from the sysroot compiler.",
    ),
    "cppo": attr.label(
        executable = True,
        cfg = "exec",
        doc = "cppo binary applied to staged x.cppo.ml{,i} sources, producing x.ml{,i} " +
              "(the cppo `(rule ...)` convention; -V OCAML:<version> is supplied).",
    ),
    "preprocess": attr.label(
        executable = True,
        cfg = "exec",
        doc = "An ocaml_ppx standalone driver run over every source before compilation " +
              "(dune `(preprocess (pps ...))`).",
    ),
    "preprocess_runtime_deps": attr.label_list(
        providers = [OcamlInfo],
        doc = "Runtime libraries the rewriter's output requires (e.g. ppx_deriving.runtime); " +
              "merged into deps.",
    ),
    "_driver": attr.label(
        default = "//bazel/ocaml/driver:ocaml_compile",
        executable = True,
        cfg = "exec",
    ),
}

# Runnable rules (binary/test) additionally take `data`; ocaml_library does not,
# since its implementation has no runfiles to put them in.
_RUNNABLE_ATTRS = dict(_COMMON_ATTRS, data = attr.label_list(
    allow_files = True,
    doc = "Runtime files made available in the runfiles tree (test corpora etc.).",
))

# Library-only attrs: wrapping is meaningless for binaries/tests (their modules
# are not a consumable namespace), so only ocaml_library carries it.
_LIBRARY_ATTRS = dict(_COMMON_ATTRS, wrapped = attr.bool(
    default = False,
    doc = "Dune-style wrapping: members compile as <Lib>__<Module> behind a " +
          "generated alias module, with -open <Lib>__. Matches dune's default " +
          "(wrapped true). Our first-party default is False (Semgrep house style).",
))

ocaml_library = rule(
    implementation = _ocaml_library_impl,
    attrs = _LIBRARY_ATTRS,
    toolchains = [_TOOLCHAIN_TYPE],
    doc = "Compile a set of .ml/.mli modules into a native .cmxa archive.",
)

ocaml_binary = rule(
    implementation = _ocaml_binary_impl,
    attrs = _RUNNABLE_ATTRS,
    toolchains = [_TOOLCHAIN_TYPE],
    executable = True,
    doc = "Compile + link a runnable native OCaml executable.",
)

# An OCaml test is just a native executable that exits 0 on success and non-zero
# on failure (the same convention as Dune's `(test)` stanza). The linked binary
# *is* the test runner, so `bazel test //...` runs it directly and the exit code
# is the verdict — no wrapper script. Shares the binary implementation.
ocaml_test = rule(
    implementation = _ocaml_binary_impl,
    attrs = _RUNNABLE_ATTRS,
    toolchains = [_TOOLCHAIN_TYPE],
    test = True,
    doc = "Compile + link a native OCaml test executable (exit 0 = pass).",
)

def _ocaml_ppx_impl(ctx):
    """Link a ppxlib standalone driver from rewriter libraries.

    The whole main is `Ppxlib.Driver.standalone ()`; the rewriters in `deps`
    register themselves at module-init time, so linking them in is all the
    composition there is. The driver executable runs at build time, hence
    consumers reference it with cfg = "exec" (the `preprocess` attr).
    """
    tc = ctx.toolchains[_TOOLCHAIN_TYPE].ocaml
    dep = _collect(ctx)

    main = ctx.actions.declare_file(ctx.label.name + "_driver_main.ml")
    ctx.actions.write(main, "let () = Ppxlib.Driver.standalone ()\n")

    exe = ctx.actions.declare_file(ctx.label.name)
    args = _driver_args(ctx, tc, "binary", dep.includes.to_list(), dep.opam.to_list(), [main], [])
    args.add("--exe-out", exe.path)

    # -linkall: rewriters register themselves with ppxlib at module init; the
    # generated main references none of them, so a normal link would drop them.
    args.add("--linkall", "1")
    for c in dep.cmxa.to_list():  # postorder: dependencies before dependents
        args.add("--cmxa", c.path)

    ctx.actions.run(
        executable = ctx.executable._driver,
        arguments = [args],
        inputs = depset([main], transitive = [dep.includes, dep.cmxa, dep.a, tc.sysroot_files]),
        outputs = [exe],
        mnemonic = "OcamlPpxDriver",
        progress_message = "Linking ppx driver %{label}",
    )
    return [DefaultInfo(files = depset([exe]), executable = exe)]

# ocaml_ppx takes no srcs (the main is generated) and no preprocessors of its
# own; everything else mirrors the common attr set.
_PPX_ATTRS = {k: v for k, v in _COMMON_ATTRS.items() if k in ("deps", "opam_deps", "ocamlopt_flags", "_driver")}

ocaml_ppx = rule(
    implementation = _ocaml_ppx_impl,
    attrs = _PPX_ATTRS,
    toolchains = [_TOOLCHAIN_TYPE],
    executable = True,
    doc = "Links a ppxlib standalone driver executable from ppx rewriter libraries.",
)
