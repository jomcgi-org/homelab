load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "gazelle",
    srcs = [
        "config.go",
        "generate.go",
        "language.go",
        "sca.go",
    ],
    importpath = "github.com/jomcgi/homelab/bazel/semgrep/defs/gazelle",
    visibility = ["//:__pkg__"],
    deps = [
        "@gazelle//config",
        "@gazelle//label",
        "@gazelle//language",
        "@gazelle//repo",
        "@gazelle//resolve",
        "@gazelle//rule",
    ],
)

go_test(
    name = "gazelle_test",
    srcs = [
        "config_extra_test.go",
        "config_test.go",
        "generate_extra_test.go",
        "generate_helpers_test.go",
        "generate_test.go",
        "language_test.go",
        "sca_test.go",
    ],
    embed = [":gazelle"],
    deps = [
        "@gazelle//config",
        "@gazelle//label",
        "@gazelle//language",
        "@gazelle//rule",
    ],
)
