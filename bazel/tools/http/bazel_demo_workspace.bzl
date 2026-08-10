"""bazel_demo_workspace - vendor the Abseil checkout + its dep distdir for the skyframe demo.

The EmberVM bazel-query demo guest (ADR embervm/010) is zero-egress: the warming
`bazel cquery //absl/...` that runs at base-build time, and every serving query that
runs in a restored clone, must fetch NOTHING from the network. Abseil is pinned to
LTS 20240116.2 and run in WORKSPACE mode (`--noenable_bzlmod`) with a `--distdir` of
checksummed archives, so all external deps resolve offline. This repo rule bakes, into
one arch-independent tar layer the apko_image consumes via `multiarch_tars`:

  - the Abseil release archive, `download_and_extract`ed so the source tree lands at
    `/opt/abseil/` in-image (the top-level `abseil-cpp-<ver>/` prefix is stripped by
    download_and_extract's `stripPrefix`); and
  - each external dep archive Abseil's WORKSPACE fetches, downloaded VERBATIM (not
    extracted) to `/opt/distdir/<original-upstream-filename>`. `--distdir` matches by
    filename + sha256, so the upstream filenames MUST be preserved exactly.

The repo rule stages both under a single `root/` dir laid out exactly as the in-image
tree (root/opt/abseil, root/opt/distdir), then tars it IN THE REPOSITORY RULE via
stage_layer_tar.sh (repo rules run outside the sandbox on the real staged tree, so
there is no $(location)/genrule-prerequisite plumbing and no sandbox symlinks to
dereference). That script pins every field a tar would otherwise take from the
building machine; see its header, and #4594 for what drifted.
It exposes the single resulting `layer.tar` under `:tar_amd64` /
`:tar_arm64` filegroups plus a `:tar` alias tagged `multiarch_tar`, mirroring
k3s_archive: apko_image appends `_amd64` / `_arm64` to the base label, so the image
references `@bazel_demo_workspace//:tar`. The content is IDENTICAL across arches
(Abseil source + dep archives are arch-independent), so one tar is exposed under both
names; the arch split exists only to satisfy apko's label-suffixing convention.

The dep set below was enumerated by running the warming cquery against Abseil 20240116.2
under Bazel 7.4.1 and recording every http_archive that actually materialized in the
output base (Bazel-bundled repos like rules_java_builtin and the *_toolchain_config
repos are symlinks into the install base, not network downloads, so they are excluded).
Offline viability was proven: with the global repo cache cleared and the network blocked,
the cquery completes using ONLY this distdir (zero "Fetching" lines, 514 targets analyzed).

  distdir archives (name, url, sha256):
    bazel-skylib-1.5.0.tar.gz
      https://github.com/bazelbuild/bazel-skylib/releases/download/1.5.0/bazel-skylib-1.5.0.tar.gz
      cd55a062e763b9349921f0f5db8c3933288dc8ba4f76dd9416aac68acee3cb94
    v1.8.3.tar.gz  (google/benchmark 1.8.3)
      https://github.com/google/benchmark/archive/refs/tags/v1.8.3.tar.gz
      6bc180a57d23d4d9515519f92b0c83d61b05b5bab188961f36ac7b06b0d9e9ce
    v1.14.0.tar.gz  (google/googletest 1.14.0)
      https://github.com/google/googletest/archive/refs/tags/v1.14.0.tar.gz
      8ad598c73ad796e0d8280b082cebd82a630d73e73cd3c70057938a6501bba5d7
    platforms-0.0.8.tar.gz
      https://github.com/bazelbuild/platforms/releases/download/0.0.8/platforms-0.0.8.tar.gz
      8150406605389ececb6da07cbcb509d5637a3ab9a24bc69b1101531367d89d74
    rules_cc-0.0.9.tar.gz
      https://github.com/bazelbuild/rules_cc/releases/download/0.0.9/rules_cc-0.0.9.tar.gz
      2037875b9a4456dce4a79d112a8ae885bbc4aad968e6587dca6e64f3a0900cdf
    rules_python-0.24.0.tar.gz
      https://github.com/bazelbuild/rules_python/releases/download/0.24.0/rules_python-0.24.0.tar.gz
      0a8003b044294d7840ac7d9d73eef05d6ceb682d7516781a4ec62eeb34702578
"""

# In-image destination paths, laid out under the staging root/ dir. Content is
# arch-independent (Abseil source + dep archives), so both per-arch tars are identical.
_ABSEIL_DIR = "opt/abseil"
_DISTDIR = "opt/distdir"

def _basename(url):
    """The final path segment of a URL, used as the on-disk distdir filename.

    --distdir matches archives by filename + sha256, so this MUST equal the upstream
    asset filename (e.g. github archive tags download as v<ver>.tar.gz)."""
    return url.rsplit("/", 1)[-1]

def _bazel_demo_workspace_impl(repository_ctx):
    # Stage everything under root/ laid out exactly as the in-image tree, so the
    # tar step below is a plain `tar -C root .` with no path surgery.

    # Extract the Abseil release directly to root/opt/abseil (stripPrefix drops the
    # top-level abseil-cpp-<ver>/ so the tree root is /opt/abseil in-image).
    repository_ctx.download_and_extract(
        url = repository_ctx.attr.abseil_url,
        sha256 = repository_ctx.attr.abseil_sha256,
        stripPrefix = repository_ctx.attr.abseil_strip_prefix,
        output = "root/" + _ABSEIL_DIR,
    )

    # Download each dep archive VERBATIM (not extracted) to root/opt/distdir, keeping
    # the upstream filename so --distdir's filename+sha256 match holds in-image.
    urls = repository_ctx.attr.distdir_urls
    sha256s = repository_ctx.attr.distdir_sha256s
    if len(urls) != len(sha256s):
        fail("distdir_urls and distdir_sha256s must be the same length: %d vs %d" % (len(urls), len(sha256s)))

    for i in range(len(urls)):
        repository_ctx.download(
            url = urls[i],
            sha256 = sha256s[i],
            output = "root/" + _DISTDIR + "/" + _basename(urls[i]),
        )

    # Build the tar HERE, in the repository rule, rather than in a genrule. The
    # earlier genrule approach was fragile twice over: a $(location root/.../WORKSPACE)
    # anchor is only legal if that exact file is a declared prerequisite (it was
    # not, so analysis failed), and any dirname-counting anchor can silently place
    # the layer at the wrong depth. A repository rule runs OUTSIDE the sandbox on
    # the real staged tree, so archiving root/ is exact and needs no $(location),
    # no prerequisite plumbing, and no symlink dereferencing (root/ holds real
    # files, not sandbox symlinks). The content is arch-independent (Abseil source
    # + dep archives), so both per-arch tars are byte-identical; we produce ONE tar
    # and expose it under both arch names via filegroups for apko_image's
    # multiarch_tars label-suffixing (_amd64 / _arm64).
    #
    # The tar archives root/'s CONTENTS (opt/abseil, opt/distdir) at the tar
    # root, so the layer unpacks to /opt/abseil + /opt/distdir in-image.
    #
    # It MUST be byte-reproducible across machines. This runs in a REPOSITORY
    # RULE, so it re-executes on every runner that fetches the repo fresh, and
    # anything machine-derived it records becomes a different embervm image
    # digest per runner. That is issue #4594: the missed-bump guard failed PRs
    # for a rebuild that never happened.
    #
    # The archiving itself lives in stage_layer_tar.sh so it can be unit tested,
    # which is what the first attempt at this lacked: #4598 sorted the member
    # list but left tar recursing into the directories in that list, so the
    # sort was inert and the layer stayed in readdir order (and held 13113
    # entries for 1601 unique paths). Referencing the script by Label also makes
    # the repo re-fetch when the archiving changes.
    layer_script = repository_ctx.path(Label("//bazel/tools/http:stage_layer_tar.sh"))
    tar_result = repository_ctx.execute(["sh", str(layer_script), "root", "layer.tar"])
    if tar_result.return_code != 0:
        fail("bazel_demo_workspace: tar of staged root/ failed (%d): %s" % (
            tar_result.return_code,
            tar_result.stderr,
        ))

    # One byte-identical tar exposed under both arch names. apko_image appends
    # _amd64 / _arm64 to the base label, so it targets :tar_amd64 / :tar_arm64; the
    # :tar alias (tagged multiarch_tar) mirrors the k3s_archive / multiarch_http_file
    # convention.
    build_content = """# Generated by bazel_demo_workspace
filegroup(
    name = "tar_amd64",
    srcs = ["layer.tar"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "tar_arm64",
    srcs = ["layer.tar"],
    visibility = ["//visibility:public"],
)

alias(
    name = "tar",
    actual = ":tar_amd64",
    tags = ["multiarch_tar"],
    visibility = ["//visibility:public"],
)
"""

    repository_ctx.file("BUILD.bazel", build_content)

bazel_demo_workspace = repository_rule(
    implementation = _bazel_demo_workspace_impl,
    attrs = {
        "abseil_url": attr.string(
            mandatory = True,
            doc = "URL for the Abseil LTS release archive (.tar.gz)",
        ),
        "abseil_sha256": attr.string(
            mandatory = True,
            doc = "SHA256 of the Abseil release archive",
        ),
        "abseil_strip_prefix": attr.string(
            mandatory = True,
            doc = "Top-level dir to strip from the Abseil archive (e.g. abseil-cpp-20240116.2)",
        ),
        "distdir_urls": attr.string_list(
            mandatory = True,
            doc = "URLs of the external dep archives; the upstream filename is preserved verbatim in /opt/distdir",
        ),
        "distdir_sha256s": attr.string_list(
            mandatory = True,
            doc = "SHA256s of the dep archives, parallel to distdir_urls",
        ),
    },
    doc = """Vendor an Abseil checkout at /opt/abseil plus a /opt/distdir of its external
dep archives, emitted as an arch-independent tar layer for apko_image's multiarch_tars.

Usage in MODULE.bazel:

    bazel_demo_workspace = use_repo_rule("//bazel/tools/http:bazel_demo_workspace.bzl", "bazel_demo_workspace")
    bazel_demo_workspace(
        name = "bazel_demo_workspace",
        abseil_url = "https://github.com/abseil/abseil-cpp/releases/download/20240116.2/abseil-cpp-20240116.2.tar.gz",
        abseil_sha256 = "...",
        abseil_strip_prefix = "abseil-cpp-20240116.2",
        distdir_urls = ["https://.../bazel-skylib-1.5.0.tar.gz", ...],
        distdir_sha256s = ["cd55...", ...],
    )

Then in an apko_image BUILD:

    apko_image(name = "image", multiarch_tars = ["@bazel_demo_workspace//:tar"])
""",
)
