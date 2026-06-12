"""Module extension that fetches the pinned Semgrep CE tree -> @semgrep_src.

Same shape as toolchain/repositories.bzl (@ocaml_source): shallow clone of
the pinned commit, .git dropped. On top of that, wave D's translation
frontier: overlay files replace the tree paths listed in source.bzl (each
overlay documents its reason), then dune2bazel runs over SEMGREP_SRC_DIRS
to generate the repo BUILD, resolving (libraries ...) against the opam
lock's libs tables plus the already-translated internal libraries
(SEMGREP_LIBS). Anything the translator does not model still rejects
loudly at fetch time.
"""

load(
    ":source.bzl",
    "OVERLAYS",
    "SEMGREP_COMMIT",
    "SEMGREP_GIT_URL",
    "SEMGREP_LIBS",
    "SEMGREP_SRC_DIRS",
)

_ROOT_MODULE = "homelab"

def _run(ctx, args, **kwargs):
    result = ctx.execute(args, **kwargs)
    if result.return_code != 0:
        fail("semgrep_src fetch: `%s` failed (exit %d):\n%s%s" % (
            " ".join([str(a) for a in args]),
            result.return_code,
            result.stdout,
            result.stderr,
        ))
    return result

def _semgrep_src_impl(ctx):
    _run(ctx, ["git", "init", "-q"])
    _run(ctx, ["git", "remote", "add", "origin", SEMGREP_GIT_URL])
    _run(ctx, ["git", "fetch", "-q", "--depth", "1", "origin", SEMGREP_COMMIT], timeout = 900)
    _run(ctx, ["git", "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"])

    head = _run(ctx, ["git", "rev-parse", "HEAD"]).stdout.strip()
    if head != SEMGREP_COMMIT:
        fail("semgrep_src: checked out %s, expected pinned %s" % (head, SEMGREP_COMMIT))
    _run(ctx, ["rm", "-rf", ".git"])

    # Apply overlays: each listed tree path is replaced by the checked-in
    # overlays/<path> file (commented with what it changes and why).
    for path in OVERLAYS:
        overlay = ctx.path(Label("//bazel/ocaml/semgrep_src:overlays/%s" % path))
        ctx.file(path, ctx.read(overlay))

    # lib_map: opam lock libs tables (same composition as opam/extension.bzl)
    # plus the internal libraries translated so far.
    lock = json.decode(ctx.read(ctx.path(Label("//bazel/ocaml/opam:lock.json"))))
    lib_map = {}
    for pkg in lock["packages"]:
        for findlib, target in pkg.get("libs", {}).items():
            lib_map[findlib] = "@%s//:%s" % (pkg["repo"], target)
    ppx_runtime_map = {}
    for pkg in lock["packages"]:
        for pps_name, runtime_names in pkg.get("ppx_runtime", {}).items():
            ppx_runtime_map[pps_name] = [lib_map[rn] for rn in runtime_names]
    for dune_name, target in SEMGREP_LIBS.items():
        lib_map[dune_name] = target

    generator = ctx.path(Label("//bazel/ocaml/opam:dune2bazel.py"))
    cmd = [
        "python3",
        generator,
        "--root",
        _ROOT_MODULE,
        "--lib-map-json",
        json.encode(lib_map),
        "--ppx-runtime-map-json",
        json.encode(ppx_runtime_map),
    ]
    for d in SEMGREP_SRC_DIRS:
        if not ctx.path(d + "/dune").exists:
            fail("semgrep_src: %s/dune not found after checkout" % d)
        cmd += ["--dune-dir", d]
    result = ctx.execute(cmd, timeout = 60)
    if result.return_code != 0:
        fail("dune2bazel failed for semgrep_src:\n%s%s" % (result.stdout, result.stderr))

    ctx.file("BUILD.bazel", result.stdout)

_semgrep_src_repo = repository_rule(
    implementation = _semgrep_src_impl,
    doc = "Clones the pinned Semgrep CE tree and translates its frontier dune dirs.",
)

def _extension_impl(_mctx):
    _semgrep_src_repo(name = "semgrep_src")

semgrep_src = module_extension(implementation = _extension_impl)
