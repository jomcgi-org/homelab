load("@rules_go//go:def.bzl", "go_library", "go_test")

filegroup(
    name = "snapshot_fixture",
    srcs = ["testdata/summits.json"],
    visibility = [
        "//projects/monolith:__pkg__",
        "//projects/scotland-connector:__subpackages__",
    ],
)

go_library(
    name = "summits",
    srcs = ["summits.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/summits",
    visibility = ["//projects/scotland-connector:__subpackages__"],
)

go_test(
    name = "summits_test",
    srcs = ["summits_test.go"],
    data = glob(["testdata/**"]),
    embed = [":summits"],
    embedsrcs = ["testdata/summits.json"],
)
