"""Hermetic CXX toolchain rule using a downloaded LLVM distribution.

Copied from loom/toolchains/cxx_dist.bzl. Provides clang/clang++/llvm-ar/lld from
a pinned LLVM dist so linking needs no system C toolchain on the build runner —
the Rust example links through this (via system_cxx_toolchain) against rust's
self-contained musl crt, so no glibc/libc-dev is required either.
"""

load("@prelude//cxx:cxx_toolchain_types.bzl", "LinkerType")
load("@prelude//toolchains:cxx.bzl", "CxxToolsInfo")

def _hermetic_cxx_tools_impl(ctx: AnalysisContext) -> list[Provider]:
    llvm_dist = ctx.attrs.llvm_dist[DefaultInfo].default_outputs[0]

    return [
        DefaultInfo(),
        CxxToolsInfo(
            compiler = cmd_args(llvm_dist, format = "{}/bin/clang"),
            compiler_type = "clang",
            cxx_compiler = cmd_args(llvm_dist, format = "{}/bin/clang++"),
            asm_compiler = cmd_args(llvm_dist, format = "{}/bin/clang"),
            asm_compiler_type = "clang",
            rc_compiler = None,
            cvtres_compiler = None,
            archiver = cmd_args(llvm_dist, format = "{}/bin/llvm-ar"),
            archiver_type = "gnu",
            linker = cmd_args(llvm_dist, format = "{}/bin/clang++"),
            linker_type = LinkerType("gnu"),
        ),
    ]

hermetic_cxx_tools = rule(
    impl = _hermetic_cxx_tools_impl,
    attrs = {
        "llvm_dist": attrs.dep(),
    },
)
