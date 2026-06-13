"""Hermetic Rust toolchain for homelab's buck2 examples.

A trimmed version of loom/toolchains/rust_dist.bzl — same `hermetic_rust_toolchain`
primitive + `RustToolchainInfo` wiring, so examples are representative of how loom
builds Rust. Clippy is dropped (examples don't lint). Linking goes through the
prelude's cxx toolchain (the prelude always drives the linker from there), so the
build host needs a `cc` (provided by `build-essential` in CI). Rust images
therefore build on linux (CI/RE), not on a macOS host.
"""

load("@prelude//rust/rust_toolchain.bzl", "PanicRuntime", "RustToolchainInfo")

def _hermetic_rust_toolchain_impl(ctx: AnalysisContext) -> list[Provider]:
    rustc_dist = ctx.attrs.rustc_dist[DefaultInfo].default_outputs[0]
    std_dist = ctx.attrs.std_dist[DefaultInfo].default_outputs[0]

    rustc = cmd_args(rustc_dist, format = "{}/bin/rustc")
    rustdoc = cmd_args(rustc_dist, format = "{}/bin/rustdoc")

    # Assemble a sysroot: rustc expects lib/rustlib/<target>/lib under --sysroot.
    # Copy rustc's own libs + the target std into the expected layout.
    sysroot = ctx.actions.declare_output("sysroot", dir = True)
    target = ctx.attrs.rustc_target_triple

    ctx.actions.run(
        cmd_args(
            "bash",
            "-c",
            cmd_args(
                "set -euo pipefail;",
                "SYSROOT=\"$1\"; RUSTC_DIST=\"$2\"; STD_DIST=\"$3\"; TARGET=\"$4\";",
                "mkdir -p \"$SYSROOT\"/lib/rustlib/\"$TARGET\";",
                "cp -rfL \"$RUSTC_DIST\"/lib/* \"$SYSROOT\"/lib/ 2>/dev/null || true;",
                "cp -rfL \"$STD_DIST\"/lib/rustlib/\"$TARGET\"/* \"$SYSROOT\"/lib/rustlib/\"$TARGET\"/;",
                delimiter = " ",
            ),
            "_",  # dummy $0
            sysroot.as_output(),
            rustc_dist,
            std_dist,
            target,
        ),
        category = "assemble_sysroot",
        allow_cache_upload = True,
    )

    return [
        DefaultInfo(),
        RustToolchainInfo(
            compiler = RunInfo(args = [rustc]),
            rustdoc = RunInfo(args = [rustdoc]),
            # examples don't run clippy; point it at rustc as a harmless fallback.
            clippy_driver = RunInfo(args = [rustc]),
            panic_runtime = PanicRuntime("unwind"),
            default_edition = ctx.attrs.default_edition,
            rustc_target_triple = target,
            sysroot_path = sysroot,
            rustc_flags = ctx.attrs.rustc_flags,
        ),
    ]

hermetic_rust_toolchain = rule(
    impl = _hermetic_rust_toolchain_impl,
    is_toolchain_rule = True,
    attrs = {
        "rustc_dist": attrs.dep(),
        "std_dist": attrs.dep(),
        "rustc_target_triple": attrs.string(default = "x86_64-unknown-linux-gnu"),
        "default_edition": attrs.string(default = "2024"),
        "rustc_flags": attrs.list(attrs.string(), default = []),
    },
)
