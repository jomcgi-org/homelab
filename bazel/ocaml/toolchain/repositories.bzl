"""Module extension that builds the hermetic OCaml compiler from source.

Clones the pinned Semgrep OCaml fork (source.bzl), then `./configure && make &&
make install` into `@ocaml_sysroot//:sysroot` — a filegroup the ocaml rules stage
as action inputs, so the compiler travels with the action and works regardless of
where BuildBuddy schedules it (the per-action container-image property is not
honored on this RBE, hence this approach).

Why build from source instead of fetching debs? See source.bzl: it matches
Semgrep's exact 5.3.0 compiler and ships `compiler-libs` (which the Debian debs
stripped), unblocking ppx. The compiler relocates via an OCAMLLIB override; native
code generation and linking use the execution host's as/gcc/ld (the same C
toolchain the repo's C/C++ builds use), so no C toolchain is bundled.

The first build is slow (~minutes); Bazel caches the repository output. A pinned
relocatable prebuilt is the planned follow-up if CI fetch time becomes a problem.
"""

load(":source.bzl", "OCAML_COMMIT", "OCAML_GIT_URL")

_BUILD = """\
filegroup(
    name = "sysroot",
    srcs = glob([
        "sysroot/bin/**",
        "sysroot/lib/ocaml/**",
    ]),
    visibility = ["//visibility:public"],
)
"""

def _run(ctx, args, **kwargs):
    result = ctx.execute(args, **kwargs)
    if result.return_code != 0:
        fail("OCaml sysroot: `%s` failed (exit %d):\n%s%s" % (
            " ".join([str(a) for a in args]),
            result.return_code,
            result.stdout,
            result.stderr,
        ))
    return result

def _ocaml_sysroot_impl(ctx):
    # Shallow-fetch the pinned commit of the Semgrep OCaml fork.
    _run(ctx, ["git", "init", "-q"])
    _run(ctx, ["git", "remote", "add", "origin", OCAML_GIT_URL])
    _run(ctx, ["git", "fetch", "-q", "--depth", "1", "origin", OCAML_COMMIT], timeout = 900)
    _run(ctx, ["git", "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"])

    head = _run(ctx, ["git", "rev-parse", "HEAD"]).stdout.strip()
    if head != OCAML_COMMIT:
        fail("OCaml sysroot: checked out %s, expected pinned %s" % (head, OCAML_COMMIT))

    prefix = str(ctx.path("sysroot"))
    nproc = ctx.execute(["nproc"])
    jobs = nproc.stdout.strip() if nproc.return_code == 0 else "2"

    # Build + install only the native compiler + stdlib + compiler-libs.
    _run(ctx, ["./configure", "--prefix", prefix], timeout = 1200)
    _run(ctx, ["make", "-j" + jobs], timeout = 3600)
    _run(ctx, ["make", "install"], timeout = 900)

    if not ctx.path("sysroot/bin/ocamlopt.opt").exists:
        fail("OCaml sysroot: ocamlopt.opt missing after build + install")
    if not ctx.path("sysroot/lib/ocaml/compiler-libs/ocamlcommon.cmxa").exists:
        fail("OCaml sysroot: compiler-libs missing after install (ppx would be blocked)")

    ctx.file("BUILD.bazel", _BUILD)

_ocaml_sysroot_repo = repository_rule(
    implementation = _ocaml_sysroot_impl,
    doc = "Builds the pinned Semgrep OCaml fork from source into a relocatable sysroot.",
)

def _extension_impl(_mctx):
    _ocaml_sysroot_repo(name = "ocaml_sysroot")

ocaml_sysroot = module_extension(implementation = _extension_impl)
