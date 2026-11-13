load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "roadworks",
    srcs = ["roadworks.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/internal/roadworks",
    visibility = ["//projects/scotland-connector:__subpackages__"],
    deps = ["//projects/scotland-connector/internal/stars"],
)

go_test(
    name = "roadworks_test",
    srcs = ["roadworks_test.go"],
    data = glob(["testdata/**"]),
    deps = [
        ":roadworks",
        "//projects/scotland-connector/internal/fixture",
    ],
)
