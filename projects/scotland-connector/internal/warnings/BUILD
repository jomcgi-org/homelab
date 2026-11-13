load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "warnings",
    srcs = [
        "producer.go",
        "warnings.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/warnings",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = ["//projects/scotland-connector/internal/verdict"],
)

go_test(
    name = "warnings_test",
    srcs = ["warnings_test.go"],
    data = glob(["testdata/**"]),
    embed = [":warnings"],
    deps = ["//projects/scotland-connector/internal/verdict"],
)

# Fixture provenance participates in the repository documentation manifest.
filegroup(
    name = "repo_docs",
    srcs = ["testdata/README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
