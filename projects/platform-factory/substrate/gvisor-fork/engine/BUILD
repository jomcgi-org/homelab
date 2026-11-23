# gazelle:ignore
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The image build is not a Bazel build: build.sh runs in the "Substrate fork
# engine image" workflow (apko and Bazel images push on merge, and this image
# must publish only when asked). This test covers what needs no Docker.
py_test(
    name = "engine_test",
    srcs = ["engine_test.py"],
    data = glob(
        ["*"],
        exclude = [
            "BUILD",
            "engine_test.py",
        ],
    ),
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

# Markdown read by the monolith doc-manifest genrules. See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)

# The sources the Pro engine image (../engine-pro) builds from and tests with:
# the server, the pins and the client scripts are shared, not copied.
filegroup(
    name = "shared_sources",
    srcs = [
        "edge.py",
        "forksrv-shim.c",
        "pins.env",
        "smoke.py",
        "uname.c",
    ],
    visibility = ["//projects/platform-factory/substrate/gvisor-fork/engine-pro:__pkg__"],
)
