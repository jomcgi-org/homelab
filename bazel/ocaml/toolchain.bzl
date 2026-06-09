"""OCaml toolchain — Bazel toolchain mechanism for the bazel/ocaml ruleset.

The compiler is supplied as a *hermetic sysroot staged into the action* (see
toolchain/repositories.bzl): the pinned Semgrep OCaml fork (5.3.0) is built from
source into `@ocaml_sysroot//:sysroot` and fed to every ocaml action as inputs.
Relocation to wherever Bazel stages it is a single OCAMLLIB override. Native
linking uses the execution host's gcc/as/ld (the same C toolchain the repo's
C/C++ builds use) — so no C toolchain is bundled. Building from source (rather
than fetching debs) matches Semgrep's compiler and ships compiler-libs, which
unblocks ppx.

Why not a container image? BuildBuddy's RBE here does not honor the per-action
`container-image` execution property (verified: actions land on the default
executor regardless), so the toolchain cannot live in an image — it has to
travel with the action. See bazel/ocaml/README.md.

OcamlToolchainInfo carries the sysroot files plus tool configuration consumed by
the rule implementations.
"""

OcamlToolchainInfo = provider(
    doc = "Hermetic OCaml compiler sysroot + tool configuration.",
    fields = {
        "sysroot_files": "depset[File]: the extracted OCaml compiler sysroot, staged as action inputs.",
        "use_ocamlfind": "If True, drive compilation via ocamlfind and resolve opam_deps as findlib packages; else use the compiler directly with stdlib-shipped archives.",
        "extra_compile_flags": "Extra flags passed to every ocamlopt compile.",
    },
)

def _ocaml_toolchain_impl(ctx):
    return [platform_common.ToolchainInfo(
        ocaml = OcamlToolchainInfo(
            sysroot_files = depset(ctx.files.sysroot),
            use_ocamlfind = ctx.attr.use_ocamlfind,
            extra_compile_flags = ctx.attr.extra_compile_flags,
        ),
    )]

ocaml_toolchain = rule(
    implementation = _ocaml_toolchain_impl,
    attrs = {
        "sysroot": attr.label(
            default = "@ocaml_sysroot//:sysroot",
            allow_files = True,
            doc = "The extracted OCaml compiler sysroot filegroup.",
        ),
        "use_ocamlfind": attr.bool(default = False),
        "extra_compile_flags": attr.string_list(default = []),
    },
    doc = "Declares the OCaml toolchain. Register an instance with register_toolchains().",
)
