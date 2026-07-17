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
        "@bazel_gazelle//config:go_default_library",
        "@bazel_gazelle//label:go_default_library",
        "@bazel_gazelle//language:go_default_library",
        "@bazel_gazelle//repo:go_default_library",
        "@bazel_gazelle//resolve:go_default_library",
        "@bazel_gazelle//rule:go_default_library",
        "@io_k8s_sigs_yaml//:go_default_library",
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
        "@bazel_gazelle//config:go_default_library",
        "@bazel_gazelle//label:go_default_library",
        "@bazel_gazelle//rule:go_default_library",
    ],
)
