"""OCaml toolchain — Bazel toolchain mechanism for the bazel/ocaml ruleset.

The compiler is supplied as a *hermetic sysroot staged into the action*: the
pinned Semgrep OCaml fork (5.3.0) source is cloned (toolchain/repositories.bzl)
and built from source by the `ocaml_compiler` build action on the RBE executor
(toolchain/compiler.bzl), producing a sysroot tar fed to every ocaml action as
input; the driver extracts it and relocates the compiler with a single OCAMLLIB
override. Native linking uses the execution host's gcc/as/ld (the same C toolchain
the repo's C/C++ builds use) — so no C toolchain is bundled. Building from source
(rather than fetching debs) matches Semgrep's compiler and ships compiler-libs,
which unblocks ppx; building as an action (rather than in the repository rule)
links the executor's glibc rather than the newer runner's.

Why not a container image? BuildBuddy's RBE here does not honor the per-action
`container-image` execution property (verified: actions land on the default
executor regardless), so the toolchain cannot live in an image — it has to
travel with the action. See bazel/ocaml/README.md.

OcamlToolchainInfo carries the sysroot files plus tool configuration consumed by
the rule implementations.
"""

load("//bazel/ocaml/toolchain:arches.bzl", "OCAML_ARCHES")

OcamlToolchainInfo = provider(
    doc = "Hermetic OCaml compiler sysroot + tool configuration.",
    fields = {
        "sysroot_files": "depset[File]: the OCaml compiler sysroot tar, staged as action inputs.",
        "sysroot_tar": "File: the sysroot tar (bin/, lib/ocaml/); the driver extracts it per action.",
        "use_ocamlfind": "If True, drive compilation via ocamlfind and resolve opam_deps as findlib packages; else use the compiler directly with stdlib-shipped archives.",
        "extra_compile_flags": "Extra flags passed to every ocamlopt compile.",
    },
)

def _ocaml_toolchain_impl(ctx):
    sysroot_files = ctx.files.sysroot
    tars = [f for f in sysroot_files if f.extension == "tar"]
    if len(tars) != 1:
        fail("ocaml_toolchain: expected exactly one .tar in sysroot, got %s" %
             [f.short_path for f in sysroot_files])
    return [platform_common.ToolchainInfo(
        ocaml = OcamlToolchainInfo(
            sysroot_files = depset(sysroot_files),
            sysroot_tar = tars[0],
            use_ocamlfind = ctx.attr.use_ocamlfind,
            extra_compile_flags = ctx.attr.extra_compile_flags,
        ),
    )]

ocaml_toolchain = rule(
    implementation = _ocaml_toolchain_impl,
    attrs = {
        "sysroot": attr.label(
            default = "//bazel/ocaml/toolchain:ocaml_compiler",
            allow_files = True,
            doc = "The built OCaml compiler sysroot tar (an ocaml_compiler output).",
        ),
        "use_ocamlfind": attr.bool(default = False),
        "extra_compile_flags": attr.string_list(default = []),
    },
    doc = "Declares the OCaml toolchain. Register an instance with register_toolchains().",
)

def declare_ocaml_toolchains():
    """Declare a constrained (ocaml_toolchain, toolchain) pair per enabled arch.

    Each pair pins both target_compatible_with and exec_compatible_with to the
    arch (no cross-compilation, per ADR 006/008: an OCaml build for an arch runs
    *on* that arch), and its sysroot is the per-arch compiler build from
    declare_ocaml_sysroots(). Registration lives in MODULE.bazel, and only
    these constrained pairs are registered: an unconstrained fallback would
    match any target platform from the first (amd64) execution platform, which
    outranks all later platforms in resolution, shadowing per-arch selection
    and handing aarch64 targets x86_64 binaries (observed as Exec format error
    in the arm64 CI shard).
    """
    for arch in OCAML_ARCHES:
        if not arch.enabled:
            continue
        ocaml_toolchain(
            name = "ocaml_tools_" + arch.name,
            sysroot = "//bazel/ocaml/toolchain:ocaml_compiler_" + arch.name,
            visibility = ["//visibility:public"],
        )
        native.toolchain(
            name = "ocaml_toolchain_" + arch.name,
            exec_compatible_with = [arch.os, arch.cpu],
            target_compatible_with = [arch.os, arch.cpu],
            toolchain = ":ocaml_tools_" + arch.name,
            toolchain_type = ":toolchain_type",
            visibility = ["//visibility:public"],
        )
