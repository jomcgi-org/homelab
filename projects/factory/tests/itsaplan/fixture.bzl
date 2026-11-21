"""Fetch the pinned upstream test dependency closure, never at test runtime."""

_BUN_SHA256 = {
    "linux-x64": "93ca5a2abe45f3bcac6dd55baff8bb06c4ac824b70f89650b54e5c8b80dbef18",
    "linux-x64-musl": "284a0bfe9bd624d54c0f20e31521b26deb5c04a2c927dc2804fecd49339a948a",
    "linux-aarch64": "ab95e7e692a53d25a1baa98ee395a17f5f264aedfb3cb15811432d54142f70eb",
    "darwin-x64": "b26de5639edc6f7fbe99c6dcf8016eaa75c5b86cccfe83db30aa04bbeade8f7e",
    "darwin-aarch64": "be5f17d636d699b2eaa2c1a8ac802c87f0815ccb5fe26cbe63d2395354ae5d0d",
}

def _bun(rctx, platform, output):
    rctx.download_and_extract(
        url = "https://registry.npmjs.org/@oven/bun-{0}/-/bun-{0}-1.4.4.tgz".format(platform),
        sha256 = _BUN_SHA256[platform],
        stripPrefix = "package",
        output = output,
    )
    return str(rctx.path(output + "/bin/bun"))

def _run(rctx, args, **kwargs):
    result = rctx.execute(args, timeout = 600, **kwargs)
    if result.return_code:
        fail("itsaplan fixture failed: {}\n{}\n{}".format(args[0], result.stdout, result.stderr))

def _fixture(rctx):
    # Resolve the source label before doing work: a repository restart must not
    # leave a half-installed tree. No adapter or test code runs during fetching.
    source = rctx.path(rctx.attr.source).dirname
    manifest_patch = rctx.path(rctx.attr.manifest_patch) if rctx.attr.manifest_patch else None
    platform = ("darwin" if rctx.os.name == "mac os x" else "linux") + "-" + ("aarch64" if rctx.os.arch in ["aarch64", "arm64"] else "x64")
    if platform == "linux-x64" and rctx.path("/lib/ld-musl-x86_64.so.1").exists:
        platform += "-musl"
    runtime = _bun(rctx, "linux-x64", "runtime")
    installer = runtime if platform == "linux-x64" else _bun(rctx, platform, "installer")
    _run(rctx, ["cp", "-R", str(source) + "/.", "upstream"])
    if manifest_patch:
        _run(rctx, ["patch", "--batch", "--forward", "-p1", "--fuzz=0", "-i", str(manifest_patch)], working_directory = str(rctx.path("upstream")))
    _run(
        rctx,
        [installer, "install", "--frozen-lockfile", "--filter", rctx.attr.workspace, "--ignore-scripts", "--os=linux", "--cpu=x64", "--libc=glibc"],
        working_directory = str(rctx.path("upstream")),
        environment = {"BUN_INSTALL_CACHE_DIR": str(rctx.path("bun-cache"))},
    )

    # Preserve Bun's workspace and package links in one portable runfile. The
    # test extracts a writable private copy and overlays the chart adapter.
    _run(rctx, ["tar", "-czf", "upstream.tar.gz", "--exclude=.git", "-C", "upstream", "."])
    rctx.delete("upstream")
    rctx.delete("bun-cache")
    rctx.delete("installer")
    rctx.file("BUILD.bazel", 'exports_files(["upstream.tar.gz", "runtime/bin/bun"])\n')

itsaplan_fixture = repository_rule(
    implementation = _fixture,
    attrs = {
        "manifest_patch": attr.label(allow_single_file = True),
        "source": attr.label(mandatory = True, allow_single_file = True),
        "workspace": attr.string(default = "api", values = ["api", "web"]),
    },
)
