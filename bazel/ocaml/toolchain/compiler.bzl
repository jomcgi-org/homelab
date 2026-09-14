"""`ocaml_compiler`: build the OCaml compiler from source as a Bazel action.

The action runs on the RBE executor (the same platform the ocaml compile/link
actions run on), so the resulting binaries link the executor's glibc and run
there — fixing the `GLIBC_2.38 not found` mismatch you get when the compiler is
built in a repository rule on the newer workflow runner.

Output is a single **tar** of the `make install` prefix (`bin/`, `lib/ocaml/`,
including `compiler-libs`) plus the native tool closure used to build it. Zig,
an Alpine sysroot and GNU Make package, static Bash, and Toybox are all
checksum-locked MODULE inputs. The same tools travel in the tar so later OCaml
compile and link actions never resolve C tools from the executor PATH.
"""

load(":arches.bzl", "OCAML_ARCHES")

def _ocaml_compiler_impl(ctx):
    sysroot_tar = ctx.actions.declare_file(ctx.label.name + "_sysroot.tar")
    ctx.actions.run(
        executable = ctx.file.shell,
        arguments = [
            ctx.file._build_driver.path,
            sysroot_tar.path,
            ctx.file.configure.dirname,
            ctx.file.zig_archive.path,
            ctx.file.rootfs_archive.path,
            ctx.file.make_apk.path,
            ctx.file.bootstrap_tool.path,
            ctx.file.shell.path,
        ],
        inputs = ctx.files.srcs + [
            ctx.file._build_driver,
            ctx.file.zig_archive,
            ctx.file.rootfs_archive,
            ctx.file.make_apk,
            ctx.file.bootstrap_tool,
            ctx.file.shell,
        ],
        outputs = [sysroot_tar],
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
        "zig_archive": attr.label(mandatory = True, allow_single_file = True),
        "rootfs_archive": attr.label(mandatory = True, allow_single_file = True),
        "make_apk": attr.label(mandatory = True, allow_single_file = True),
        "shell": attr.label(mandatory = True, allow_single_file = True, cfg = "exec"),
        "bootstrap_tool": attr.label(mandatory = True, allow_single_file = True, cfg = "exec"),
        "_build_driver": attr.label(
            default = "//bazel/ocaml/toolchain:ocaml_compiler_build.sh",
            allow_single_file = True,
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
            zig_archive = "@ocaml_native_zig_%s//file" % arch.name,
            rootfs_archive = "@ocaml_native_rootfs_%s//file" % arch.name,
            make_apk = "@ocaml_native_make_%s//file" % arch.name,
            shell = "@ocaml_native_bash_%s//file" % arch.name,
            bootstrap_tool = "@ocaml_native_toybox_%s//file" % arch.name,
            exec_compatible_with = [arch.os, arch.cpu],
            visibility = ["//visibility:public"],
        )
