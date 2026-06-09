"""`ocaml_compiler`: build the OCaml compiler from source as a Bazel action.

The action runs on the RBE executor (the same platform the ocaml compile/link
actions run on), so the resulting binaries link the executor's glibc and run
there — fixing the `GLIBC_2.38 not found` mismatch you get when the compiler is
built in a repository rule on the newer workflow runner.

Output is a single TreeArtifact (`sysroot/`) holding the `make install` prefix
(`bin/`, `lib/ocaml/`, including `compiler-libs`). It is staged as inputs to every
ocaml action and relocated via OCAMLLIB. The build is hermetic (source is the only
input; `gcc`/`make` come from the executor, like the repo's C/C++ builds) and is
cached in the RBE action cache, so the compiler builds once and is reused.
"""

def _ocaml_compiler_impl(ctx):
    sysroot = ctx.actions.declare_directory(ctx.label.name + "_sysroot")
    src_root = ctx.file.configure.dirname

    command = """
set -eu
WORK="$(mktemp -d)"
cp -RL "{src_root}/." "$WORK/"
cd "$WORK"
chmod +x configure
log() {{ tail -80 "$1" >&2; }}
./configure --prefix="$WORK/_install" > _cfg.log 2>&1 || {{ log _cfg.log; exit 1; }}
make -j"$(nproc)" > _make.log 2>&1 || {{ log _make.log; exit 1; }}
make install > _inst.log 2>&1 || {{ log _inst.log; exit 1; }}
mkdir -p "{out}"
cp -RL "$WORK/_install/." "{out}/"
test -x "{out}/bin/ocamlopt.opt"
test -f "{out}/lib/ocaml/compiler-libs/ocamlcommon.cmxa"
""".format(
        src_root = src_root,
        out = sysroot.path,
    )

    ctx.actions.run_shell(
        inputs = ctx.files.srcs,
        outputs = [sysroot],
        command = command,
        use_default_shell_env = True,
        mnemonic = "OcamlCompilerBuild",
        progress_message = "Building OCaml compiler from source (%{label})",
    )

    return [DefaultInfo(files = depset([sysroot]))]

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
    doc = "Builds the OCaml compiler from source into a relocatable sysroot TreeArtifact.",
)
