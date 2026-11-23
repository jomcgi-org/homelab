# gazelle:ignore
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The image build is not a Bazel build: build.sh runs in the "Substrate fork
# engine image (Pro, private)" workflow, which needs a Semgrep token that Bazel
# and CI never hold. This test covers what needs no Docker, no network and no
# token.
py_test(
    name = "engine_pro_test",
    srcs = ["engine_pro_test.py"],
    data = glob(
        ["*"],
        exclude = [
            "BUILD",
            "engine_pro_test.py",
        ],
    ) + [
        "//:.github/workflows/substrate-fork-engine-pro.yml",
        "//projects/platform-factory/substrate/gvisor-fork/engine:shared_sources",
    ],
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
