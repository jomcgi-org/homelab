"""Module extension that builds opam packages from source via their dune files.

The pinned universe lives in lock.json (maintained by update_lock.py). For each
package the repository rule:

  1. downloads + extracts the release tarball (checksum-pinned), then
  2. generates the BUILD that drives our //bazel/ocaml rules, either by
     running dune2bazel.py over the package's own `dune` files (the default),
     or by installing a hand-written override from opam/overrides/<name>/
     (packages whose dune trees need codegen or compiler introspection the
     translator does not model -- every override is a documented TODO).

So an external opam dependency builds from its real dune metadata -- no
hand-written target -- the same hermetic-from-source approach the toolchain uses
for the compiler. The generated repo (`@ocaml_<pkg>`) loads our rules from the
root module (`@<root>//bazel/ocaml:defs.bzl`); the root module is `homelab`.

dune (libraries ...) entries that name another locked package resolve through
the lib_map: findlib name -> Bazel label, composed from each lock entry's
`libs` table. Unknown names reject loudly at fetch time.
"""

_ROOT_MODULE = "homelab"

def _opam_dune_repo_impl(ctx):
    ctx.download_and_extract(
        url = ctx.attr.url,
        sha256 = ctx.attr.sha256,
        stripPrefix = ctx.attr.strip_prefix,
        type = ctx.attr.archive_type,
    )

    if ctx.attr.build_file:
        ctx.file("BUILD.bazel", ctx.read(ctx.path(ctx.attr.build_file)))
        return

    generator = ctx.path(Label("//bazel/ocaml/opam:dune2bazel.py"))
    cmd = [
        "python3",
        generator,
        "--root",
        _ROOT_MODULE,
        "--lib-map-json",
        ctx.attr.lib_map_json,
    ]
    for d in ctx.attr.src_dirs:
        if not ctx.path(d + "/dune").exists:
            fail("opam dune repo %s: %s/dune not found after extraction" % (ctx.attr.name, d))
        cmd += ["--dune-dir", d]
    result = ctx.execute(cmd, timeout = 60)
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
        "src_dirs": attr.string_list(doc = "Subdirs holding dune files + sources to translate."),
        "build_file": attr.label(allow_single_file = True, doc = "Hand-written override BUILD installed verbatim instead of translating."),
        "lib_map_json": attr.string(doc = "JSON object: findlib library name -> Bazel label, from lock.json libs tables."),
    },
    doc = "Fetch an opam package tarball and generate its BUILD from its dune metadata.",
)

def _extension_impl(mctx):
    lock = json.decode(mctx.read(Label("//bazel/ocaml/opam:lock.json")))

    lib_map = {}
    for pkg in lock["packages"]:
        for findlib, target in pkg.get("libs", {}).items():
            lib_map[findlib] = "@%s//:%s" % (pkg["repo"], target)
    lib_map_json = json.encode(lib_map)

    for pkg in lock["packages"]:
        _opam_dune_repo(
            name = pkg["repo"],
            url = pkg["url"],
            sha256 = pkg["sha256"],
            strip_prefix = pkg["strip_prefix"],
            archive_type = pkg.get("type", "tar.bz2"),
            src_dirs = pkg.get("src_dirs", []),
            build_file = Label("//bazel/ocaml/opam/overrides:%s/BUILD.tpl" % pkg["name"]) if pkg.get("override") else None,
            lib_map_json = lib_map_json,
        )

ocaml_opam = module_extension(implementation = _extension_impl)
