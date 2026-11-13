"""Small local downloads exercising the real HTTP repository rules."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_file")
load(":multiarch_http_file.bzl", "multiarch_http_file")

def _metadata_impl(ctx):
    # Read the actual generated repositories, after their setup has executed.
    for build in ctx.attr.builds:
        content = ctx.read(build)
        for label in ["binary", "tar"] + ctx.attr.arches:
            if 'name = "%s"' % label not in content:
                fail("Missing public label %s in %s" % (label, build))
        for arch in ctx.attr.arches:
            if 'name = "tar_%s"' % arch not in content:
                fail("Missing tar for %s" % arch)
        if content.count('visibility = ["//visibility:public"]') != 2 + 2 * len(ctx.attr.arches):
            fail("Public visibility changed in %s" % build)
        preferred = "amd64" if "amd64" in ctx.attr.arches else "arm64"
        for expected in ['actual = ":%s"' % preferred, 'actual = ":tar_%s"' % preferred, 'tags = ["multiarch_tar"]']:
            if expected not in content:
                fail("Missing %s in %s" % (expected, build))
    for binary, arch in ctx.attr.binaries.items():
        result = ctx.execute([str(ctx.path(binary))])
        if result.return_code != 0 or result.stdout != "fixture-" + arch + "\n":
            fail("Repository binary execution failed: %s %s" % (binary, result.stderr))

    # Check both goose ELF files on the repository host without executing them.
    for binary in ctx.attr.executable_files:
        result = ctx.execute(["test", "-x", str(ctx.path(binary))])
        if result.return_code:
            fail("Repository binary lacks executable access: %s" % binary)
    ctx.file("checked.txt", "Generated public labels, aliases, visibility and multiarch_tar tag verified.\n")
    ctx.file("BUILD.bazel", 'exports_files(["checked.txt"], visibility = ["//visibility:public"])')

_metadata = repository_rule(
    implementation = _metadata_impl,
    attrs = {
        "builds": attr.label_list(),
        "arches": attr.string_list(),
        "binaries": attr.label_keyed_string_dict(allow_files = True),
        "executable_files": attr.label_list(allow_files = True),
    },
)

def _fixtures_impl(ctx):
    urls = {}
    for arch in ["amd64", "arm64"]:
        for suffix in ["tar", "bin"]:
            urls[arch + "." + suffix] = "file://" + str(ctx.path(Label("//bazel/tools/http:fixtures/" + arch + "." + suffix)))

    for name, arches in [
        ("http_fixture_amd64", ["amd64"]),
        ("http_fixture_arm64", ["arm64"]),
        ("http_fixture_dual", ["amd64", "arm64"]),
    ]:
        kwargs = {}
        for arch in arches:
            kwargs[arch + "_url"] = urls[arch + ".tar"]
            kwargs[arch + "_sha256"] = _HASHES[arch + ".tar"]
        multiarch_http_file(
            name = name,
            binary_name = "fixture",
            extract_path = "nested/renamed",
            package_dir = "/opt/fixture/bin",
            **kwargs
        )
        _metadata(
            name = name + "_metadata",
            builds = ["@" + name + "//:BUILD.bazel"],
            arches = arches,
            binaries = {"@" + name + "//:" + arch + "_binary": arch for arch in arches},
        )

    multiarch_http_file(
        name = "http_fixture_assets",
        amd64_url = urls["amd64.tar"],
        amd64_sha256 = _HASHES["amd64.tar"],
        binary_name = "fixture",
        extract_path = "nested/renamed",
        extract_extra_paths = ["nested/themes/", "nested/config.txt"],
        package_dir = "/opt/fixture/bin",
    )
    multiarch_http_file(
        name = "http_fixture_raw",
        arm64_url = urls["arm64.bin"],
        arm64_sha256 = _HASHES["arm64.bin"],
        binary_name = "fixture",
        package_dir = "/opt/fixture/bin",
    )

    for name, arch in [("http_fixture_assets", "amd64"), ("http_fixture_raw", "arm64")]:
        _metadata(
            name = name + "_metadata",
            builds = ["@" + name + "//:BUILD.bazel"],
            arches = [arch],
            binaries = {"@" + name + "//:" + arch + "_binary": arch},
        )

    _metadata(
        name = "goose_metadata",
        builds = ["@goose//:BUILD.bazel"],
        arches = ["amd64", "arm64"],
        executable_files = ["@goose//:amd64_binary", "@goose//:arm64_binary"],
    )

    # Independent archive inputs allow comparison with both exposed and packaged
    # goose bytes. These remain pinned identically to the production declaration.
    http_file(
        name = "http_goose_source_amd64",
        urls = ["https://github.com/block/goose/releases/download/v1.39.0/goose-x86_64-unknown-linux-gnu.tar.bz2"],
        sha256 = "cf0100d2f4e085e4ec537e8399950d4695661bff83d12dc45a9a6d357cd33c41",
        downloaded_file_path = "goose.tar.bz2",
    )
    http_file(
        name = "http_goose_source_arm64",
        urls = ["https://github.com/block/goose/releases/download/v1.39.0/goose-aarch64-unknown-linux-gnu.tar.bz2"],
        sha256 = "a0a03859f0b3810863ce212b0c11a6cf3126a074db708bf39cf46af513bdd273",
        downloaded_file_path = "goose.tar.bz2",
    )

_HASHES = {"arm64.tar": "25af35a36b6a3072c25d626c4ef26d62d6b24e5de6acaae4ab9f34e9196e8146", "amd64.bin": "3aa48791603c60c1c1b129fc5bdae2b4e6efd1fe5f1b0aaf60d28c898f18f927", "arm64.bin": "edaf23d20fe1398fbaaeeb7284431ce75f382f7e4cfdcf085a34c4ee4ee64820", "amd64.tar": "39ae671b7663d8480ca5f967461e702cdef1ab8cc1eccac4bd353621c2d1a612"}

fixtures = module_extension(implementation = _fixtures_impl)
