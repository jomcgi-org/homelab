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
        "transitive_cc_archives": "depset[File] of external C static archives (from cc_deps) linked into binaries.",
        "transitive_cc_headers": "depset[File] of cc_deps headers staged when compiling C stubs.",
        "transitive_cc_includes": "depset[str] of -I dirs exported by cc_deps for C stub compilation.",
        "transitive_cc_linkflags": "depset[str] of user link flags (e.g. -lpcre2-8) from cc_deps.",
    },
)

_TOOLCHAIN_TYPE = "//bazel/ocaml:toolchain_type"

def _collect_cc(ctx):
    """Gather C headers/includes/archives/linkflags from cc_deps + transitively.

    cc_deps (cc_library targets) supply the headers an ocaml_library's C stubs
    #include and the static archives a binary linking the library must pull in.
    Archives/headers/includes/linkflags all propagate transitively through
    OcamlInfo so a binary several deps away still links the C libraries.
    """
    inc = []          # str include dirs (direct + transitive)
    hdr = []          # depset[File]
    arch = []         # depset[File]
    flags = []        # str link flags
    direct_arch = []
    for dep in getattr(ctx.attr, "cc_deps", []):
        cc = dep[CcInfo]
        cctx = cc.compilation_context
        inc += cctx.includes.to_list() + cctx.quote_includes.to_list() + cctx.system_includes.to_list()
        hdr.append(cctx.headers)
        # Header dirnames make `#include "foo.h"` resolve without the cc_library
        # having to declare `includes`; over-inclusion is harmless.
        inc += [h.dirname for h in cctx.headers.to_list()]
        for li in cc.linking_context.linker_inputs.to_list():
            for lib in li.libraries:
                a = lib.static_library or lib.pic_static_library
                if a:
                    direct_arch.append(a)
            flags += li.user_link_flags
    arch.append(depset(direct_arch))
    for dep in ctx.attr.deps + getattr(ctx.attr, "preprocess_runtime_deps", []):
        info = dep[OcamlInfo]
        arch.append(info.transitive_cc_archives)
        hdr.append(info.transitive_cc_headers)
        inc += info.transitive_cc_includes.to_list()
        flags += info.transitive_cc_linkflags.to_list()
    return struct(
        includes = depset(inc),
        headers = depset(transitive = hdr),
        archives = depset(transitive = arch, order = "topological"),
        linkflags = depset(flags),
    )

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
    """Extra executables (preprocessors / generators) that stage as inputs."""
    files = []
    for attr in ("pp", "cppo", "preprocess", "menhir_tool"):
        tool = getattr(ctx.executable, attr, None)
        if tool:
            files.append(tool)
    return files

def _driver_args(ctx, tc, mode, include_dirs, opam_pkgs, srcs, c_srcs, cc = None):
    args = ctx.actions.args()
    args.add("--mode", mode)
    args.add("--name", ctx.label.name)
    args.add("--sysroot-tar", tc.sysroot_tar.path)
    args.add("--use-ocamlfind", "1" if tc.use_ocamlfind else "0")

    # C library integration (cc_deps): include dirs for the stub compile,
    # static archives + link flags for the final binary link.
    if cc:
        for d in cc.includes.to_list():
            args.add("--cc-include", d)
        for a in cc.archives.to_list():
            args.add("--cc-archive", a.path)
        for fl in cc.linkflags.to_list():
            args.add("--cc-linkflag", fl)

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
    if getattr(ctx.attr, "menhir", None):
        args.add("--menhir-tool", ctx.executable.menhir_tool.path)
        for m in ctx.attr.menhir:
            args.add("--menhir-module", m)
        for fl in ctx.attr.menhir_flags:
            args.add("--menhir-flag", fl)
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
    cc = _collect_cc(ctx)

    objs_dir = ctx.actions.declare_directory(ctx.label.name + "_objs")
    cmxa = ctx.actions.declare_file(ctx.label.name + ".cmxa")
    a_lib = ctx.actions.declare_file(ctx.label.name + ".a")

    args = _driver_args(ctx, tc, "library", dep.includes.to_list(), dep.opam.to_list(), ctx.files.srcs, ctx.files.c_srcs, cc = cc)
    args.add("--objs-out", objs_dir.path)
    args.add("--cmxa-out", cmxa.path)
    args.add("--a-out", a_lib.path)

    ctx.actions.run(
        executable = ctx.executable._driver,
        arguments = [args],
        inputs = depset(ctx.files.srcs + ctx.files.c_srcs + _tool_files(ctx), transitive = [dep.includes, dep.cmxa, dep.a, cc.headers, cc.archives, tc.sysroot_files]),
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
        transitive_cc_archives = cc.archives,
        transitive_cc_headers = cc.headers,
        transitive_cc_includes = cc.includes,
        transitive_cc_linkflags = cc.linkflags,
    )
    return [
        info,
        DefaultInfo(files = depset([cmxa, a_lib, objs_dir])),
    ]

def _ocaml_binary_impl(ctx):
    tc = ctx.toolchains[_TOOLCHAIN_TYPE].ocaml
    dep = _collect(ctx)
    cc = _collect_cc(ctx)

    exe = ctx.actions.declare_file(ctx.label.name)

    args = _driver_args(ctx, tc, "binary", dep.includes.to_list(), dep.opam.to_list(), ctx.files.srcs, ctx.files.c_srcs, cc = cc)
    args.add("--exe-out", exe.path)
    for c in dep.cmxa.to_list():  # postorder: dependencies before dependents
        args.add("--cmxa", c.path)

    ctx.actions.run(
        executable = ctx.executable._driver,
        arguments = [args],
        inputs = depset(ctx.files.srcs + ctx.files.c_srcs + _tool_files(ctx), transitive = [dep.includes, dep.cmxa, dep.a, cc.headers, cc.archives, tc.sysroot_files]),
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
              "binaries that link this library pull in the stubs automatically. C stubs that " +
              "#include a third-party header (pcre2.h, tree_sitter/api.h) get that header's " +
              "dir and the static archive through `cc_deps`.",
    ),
    "cc_deps": attr.label_list(
        providers = [CcInfo],
        doc = "cc_library targets whose headers this library's C stubs #include and whose " +
              "static archives binaries linking this library must pull in (dune's " +
              "`foreign_archives` / `c_library_flags`). Propagated transitively.",
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
    "menhir": attr.string_list(
        doc = "Grammar module names whose <Module>.mly (in srcs) is generated by menhir " +
              "with OCaml type inference (dune `(menhir (modules ...))`).",
    ),
    "menhir_flags": attr.string_list(
        doc = "Flags passed to menhir for every grammar (dune `(menhir (flags ...))`).",
    ),
    "menhir_tool": attr.label(
        executable = True,
        cfg = "exec",
        doc = "The menhir binary (e.g. @ocaml_menhir//:menhir); required when `menhir` is set.",
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
