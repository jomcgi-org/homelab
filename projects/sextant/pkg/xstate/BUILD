load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "xstate",
    srcs = [
        "convert.go",
        "types.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/sextant/pkg/xstate",
    visibility = ["//visibility:public"],
    deps = ["//projects/sextant/pkg/schema"],
)

go_test(
    name = "xstate_test",
    srcs = [
        "convert_missing_state_test.go",
        "convert_extra_test.go",
        "convert_robustness_test.go",
        "convert_helpers_test.go",
        "convert_test.go",
        "types_test.go",
    ],
    deps = [
        ":xstate",
        "//projects/sextant/pkg/schema",
    ],
)
