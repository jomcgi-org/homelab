"""Module extension that builds opam packages from source via their dune files.

For each pinned package (packages.bzl) the repository rule:

  1. downloads + extracts the release tarball (checksum-pinned), then
  2. runs dune2bazel.py over the package's own `dune` file to generate the
     BUILD that drives our //bazel/ocaml `ocaml_library`.

So an external opam dependency builds from its real dune metadata -- no
hand-written target -- the same hermetic-from-source approach the toolchain uses
for the compiler debs. The generated repo (`@ocaml_<repo>`) loads our rules from
the root module (`@<root>//bazel/ocaml:defs.bzl`); the root module is `homelab`.
"""

load(":packages.bzl", "OPAM_PACKAGES")

_ROOT_MODULE = "homelab"

def _opam_dune_repo_impl(ctx):
    ctx.download_and_extract(
        url = ctx.attr.url,
        sha256 = ctx.attr.sha256,
        stripPrefix = ctx.attr.strip_prefix,
        type = ctx.attr.archive_type,
    )

    dune_file = ctx.attr.src_dir + "/dune"
    if not ctx.path(dune_file).exists:
        fail("opam dune repo %s: %s not found after extraction" % (ctx.attr.name, dune_file))

    generator = ctx.path(Label("//bazel/ocaml/opam:dune2bazel.py"))
    result = ctx.execute(
        ["python3", generator, dune_file, ctx.attr.src_dir, _ROOT_MODULE],
        timeout = 60,
    )
    if result.return_code != 0:
        fail("dune2bazel failed for %s:\n%s%s" % (ctx.attr.name, result.stdout, result.stderr))

    ctx.file("BUILD.bazel", result.stdout)

_opam_dune_repo = repository_rule(
    implementation = _opam_dune_repo_impl,
    attrs = {
        "url": attr.string(mandatory = True),
        "sha256": attr.string(mandatory = True),
        "strip_prefix": attr.string(mandatory = True),
        "archive_type": attr.string(default = "tar.bz2", doc = "Archive type for download_and_extract (dune-release assets are .tbz; GitHub tag tarballs are tar.gz)."),
        "src_dir": attr.string(mandatory = True, doc = "Subdir holding the library's dune file + sources."),
    },
    doc = "Fetch an opam package tarball and generate its BUILD from its dune file.",
)

def _extension_impl(_mctx):
    for pkg in OPAM_PACKAGES:
        _opam_dune_repo(
            name = pkg["repo"],
            url = pkg["url"],
            sha256 = pkg["sha256"],
            strip_prefix = pkg["strip_prefix"],
            archive_type = pkg.get("type", "tar.bz2"),
            src_dir = pkg["src_dir"],
        )

ocaml_opam = module_extension(implementation = _extension_impl)
