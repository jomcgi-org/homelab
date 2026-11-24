load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "gitleaks",
    srcs = [
        "gitleaks.go",
        "matcher.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect/gitleaks",
    visibility = ["//projects/platform-factory/substrate/egress-scan:__subpackages__"],
    deps = [
        "//projects/platform-factory/substrate/egress-scan/detect",
        "@com_github_spf13_viper//:viper",
        "@com_github_zricethezav_gitleaks_v8//config",
        "@com_github_zricethezav_gitleaks_v8//logging",
    ],
)

go_test(
    name = "gitleaks_test",
    srcs = ["gitleaks_test.go"],
    embed = [":gitleaks"],
    deps = [
        "//projects/platform-factory/substrate/egress-scan/detect",
        "//projects/platform-factory/substrate/egress-scan/detect/detecttest",
    ],
)
