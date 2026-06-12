"""`ocaml_compiler`: build the OCaml compiler from source as a Bazel action.

The action runs on the RBE executor (the same platform the ocaml compile/link
actions run on), so the resulting binaries link the executor's glibc and run
there — fixing the `GLIBC_2.38 not found` mismatch you get when the compiler is
built in a repository rule on the newer workflow runner.

Output is a single **tar** of the `make install` prefix (`bin/`, `lib/ocaml/`,
including `compiler-libs`), not a TreeArtifact: a TreeArtifact of the install does
not survive RBE staging intact (the bin tools come up missing in consuming
actions). A single File artifact always materializes whole and tar preserves the
executable bit, so the driver extracts it per action. The build is hermetic
(source is the only input; `gcc`/`make` come from the executor, like the repo's
C/C++ builds) and is cached in the RBE action cache, so the compiler builds once.
"""

load(":arches.bzl", "OCAML_ARCHES")

def _ocaml_compiler_impl(ctx):
    sysroot_tar = ctx.actions.declare_file(ctx.label.name + "_sysroot.tar")
    src_root = ctx.file.configure.dirname

    command = """
set -eu
# Absolute output path BEFORE we cd into the build dir (the path is exec-root
# relative; Bazel has already created its parent directory).
OUT="$(pwd)/{out}"
WORK="$(mktemp -d)"
cp -RL "{src_root}/." "$WORK/src"
cd "$WORK/src"
chmod +x configure
log() {{ tail -80 "$1" >&2; }}
./configure --prefix="$WORK/_install" > _cfg.log 2>&1 || {{ log _cfg.log; exit 1; }}
make -j"$(nproc)" > _make.log 2>&1 || {{ log _make.log; exit 1; }}
make install > _inst.log 2>&1 || {{ log _inst.log; exit 1; }}
test -x "$WORK/_install/bin/ocamlopt.opt"
test -f "$WORK/_install/lib/ocaml/compiler-libs/ocamlcommon.cmxa"
tar -cf "$OUT" -C "$WORK/_install" .
""".format(
        src_root = src_root,
        out = sysroot_tar.path,
    )

    ctx.actions.run_shell(
        inputs = ctx.files.srcs,
        outputs = [sysroot_tar],
        command = command,
        use_default_shell_env = True,
        mnemonic = "OcamlCompilerBuild",
        progress_message = "Building OCaml compiler from source (%{label})",
    )

    return [DefaultInfo(files = depset([sysroot_tar]))]

ocaml_compiler = rule(
    implementation = _ocaml_compiler_impl,
    attrs = {
        "srcs": attr.label(
            mandatory = True,
            allow_files = True,
            doc = "The OCaml compiler source tree (@ocaml_source//:srcs).",
        ),
        "configure": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "The source tree's ./configure script — its dir is the build root.",
        ),
    },
    doc = "Builds the OCaml compiler from source into a relocatable sysroot tar.",
)

def declare_ocaml_sysroots():
    """Declare one per-arch `ocaml_compiler` target per enabled OCAML_ARCHES entry.

    Each target carries `exec_compatible_with` for its arch, so toolchain
    resolution schedules the compiler build on that arch's executor pool (the
    platform's `Arch` routing property does the rest). That is the whole
    multi-arch story for the sysroot: the compiler is built on the executor it
    will run on, so the arm64 sysroot is just the same action landing on the
    arm64 pool -- no cross-compilation (ADR 006/008).
    """
    for arch in OCAML_ARCHES:
        if not arch.enabled:
            continue
        ocaml_compiler(
            name = "ocaml_compiler_" + arch.name,
            srcs = "@ocaml_source//:srcs",
            configure = "@ocaml_source//:configure",
            exec_compatible_with = [arch.os, arch.cpu],
            visibility = ["//visibility:public"],
        )
