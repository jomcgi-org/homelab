"""Module extension that assembles the hermetic OCaml compiler sysroot.

Downloads the pinned Debian .deb packages (debs.bzl) and extracts them into a
single repository tree via extract_debs.py. The result, `@ocaml_sysroot//:sysroot`,
is a filegroup the ocaml rules stage as action inputs — so the compiler travels
with the action and works regardless of where BuildBuddy schedules it (the per-
action container-image property is not honored on this RBE, hence this approach).
"""

load(":debs.bzl", "DEBS", "DEB_BASE_URL")

_BUILD = """\
filegroup(
    name = "sysroot",
    srcs = glob([
        "usr/bin/**",
        "usr/lib/ocaml/**",
    ]),
    visibility = ["//visibility:public"],
)
"""

def _ocaml_sysroot_impl(ctx):
    deb_paths = []
    for d in DEBS:
        out = "debs/" + d["name"] + ".deb"
        ctx.download(
            url = DEB_BASE_URL + d["filename"],
            output = out,
            sha256 = d["sha256"],
        )
        deb_paths.append(out)

    extractor = ctx.path(Label("//bazel/ocaml/toolchain:extract_debs.py"))
    result = ctx.execute(
        ["python3", extractor, "."] + deb_paths,
        timeout = 600,
    )
    if result.return_code != 0:
        fail("OCaml sysroot deb extraction failed:\n" + result.stdout + result.stderr)

    # Sanity-check the native compiler landed where the rules expect it.
    if not ctx.path("usr/bin/ocamlopt.opt").exists:
        fail("ocamlopt.opt missing from extracted OCaml sysroot")

    ctx.file("BUILD.bazel", _BUILD)

_ocaml_sysroot_repo = repository_rule(
    implementation = _ocaml_sysroot_impl,
    doc = "Downloads + extracts the pinned Debian OCaml debs into a sysroot.",
)

def _extension_impl(_mctx):
    _ocaml_sysroot_repo(name = "ocaml_sysroot")

ocaml_sysroot = module_extension(implementation = _extension_impl)
