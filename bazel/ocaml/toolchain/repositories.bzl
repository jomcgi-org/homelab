"""Module extension that fetches the OCaml compiler *source* (not a build).

Clones the pinned Semgrep OCaml fork (source.bzl) and exposes its tree as
`@ocaml_source//:srcs`. The compiler is then **built by a Bazel action** (see
toolchain/compiler.bzl), which runs on the RBE executor — so the compiler binaries
link the executor's glibc and run there. Building in a repository rule instead
would link the *workflow runner's* glibc, which is newer than the executor's and
fails at action time with `GLIBC_2.38 not found`; building as an action where it
runs is what makes it portable, and the build caches in the RBE action cache.

See source.bzl for why a from-source 5.3 build (matches Semgrep, ships
compiler-libs → unblocks ppx).
"""

load(":source.bzl", "OCAML_COMMIT", "OCAML_GIT_URL")

_BUILD = """\
filegroup(
    name = "srcs",
    srcs = glob(["**"]),
    visibility = ["//visibility:public"],
)

exports_files(["configure"])
"""

def _run(ctx, args, **kwargs):
    result = ctx.execute(args, **kwargs)
    if result.return_code != 0:
        fail("OCaml source fetch: `%s` failed (exit %d):\n%s%s" % (
            " ".join([str(a) for a in args]),
            result.return_code,
            result.stdout,
            result.stderr,
        ))
    return result

def _ocaml_source_impl(ctx):
    _run(ctx, ["git", "init", "-q"])
    _run(ctx, ["git", "remote", "add", "origin", OCAML_GIT_URL])
    _run(ctx, ["git", "fetch", "-q", "--depth", "1", "origin", OCAML_COMMIT], timeout = 900)
    _run(ctx, ["git", "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"])

    head = _run(ctx, ["git", "rev-parse", "HEAD"]).stdout.strip()
    if head != OCAML_COMMIT:
        fail("OCaml source: checked out %s, expected pinned %s" % (head, OCAML_COMMIT))

    # Drop .git so the source filegroup is just the tree, and ensure configure is
    # executable (the build action runs it directly).
    _run(ctx, ["rm", "-rf", ".git"])
    if not ctx.path("configure").exists:
        fail("OCaml source: configure missing after checkout")

    ctx.file("BUILD.bazel", _BUILD)

_ocaml_source_repo = repository_rule(
    implementation = _ocaml_source_impl,
    doc = "Clones the pinned Semgrep OCaml fork source tree (no build).",
)

def _extension_impl(_mctx):
    _ocaml_source_repo(name = "ocaml_source")

ocaml_source = module_extension(implementation = _extension_impl)
