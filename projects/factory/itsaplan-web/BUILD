# gazelle:ignore
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

exports_files(["next-security.patch"])

genrule(
    name = "standalone_tar",
    srcs = [
        "run-phases.patch",
        "@itsaplan_web_inputs//:upstream.tar.gz",
    ],
    outs = ["standalone.tar"],
    cmd = """
        $(location build.sh) \
            $(location @itsaplan_web_inputs//:upstream.tar.gz) \
            $(location run-phases.patch) \
            $(location @nodejs_linux_amd64//:node_bin) \
            $(location @itsaplan_web_inputs//:runtime/bin/bun) $@
    """,
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    tools = [
        "build.sh",
        "@itsaplan_web_inputs//:runtime/bin/bun",
        "@nodejs_linux_amd64//:node_bin",
    ],
)

apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@itsaplan_web_lock//:contents",
    tars = [":standalone_tar"],
)

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
