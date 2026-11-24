load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "detecttest",
    testonly = True,
    srcs = [
        "conformance.go",
        "corpus.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect/detecttest",
    visibility = ["//projects/platform-factory/substrate/egress-scan:__subpackages__"],
    deps = ["//projects/platform-factory/substrate/egress-scan/detect"],
)
