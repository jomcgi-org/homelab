load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "gazelle",
    srcs = [
        "config.go",
        "generate.go",
        "language.go",
    ],
    importpath = "github.com/jomcgi/homelab/bazel/helm/gazelle",
    visibility = ["//:__pkg__"],
    deps = [
        "@gazelle//config",
        "@gazelle//label",
        "@gazelle//language",
        "@gazelle//repo",
        "@gazelle//resolve",
        "@gazelle//rule",
        "@io_k8s_sigs_yaml//:yaml",
    ],
)

go_test(
    name = "gazelle_test",
    srcs = [
        "config_test.go",
        "generate_test.go",
        "language_test.go",
    ],
    embed = [":gazelle"],
    deps = [
        "@gazelle//config",
        "@gazelle//label",
        "@gazelle//rule",
    ],
)
