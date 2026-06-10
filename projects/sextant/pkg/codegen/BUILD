load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "codegen",
    srcs = ["generator.go"],
    embedsrcs = [
        "templates/calculator.go.tmpl",
        "templates/metrics.go.tmpl",
        "templates/observability.go.tmpl",
        "templates/phases.go.tmpl",
        "templates/status.go.tmpl",
        "templates/transitions.go.tmpl",
        "templates/types.go.tmpl",
        "templates/visit.go.tmpl",
    ],
    importpath = "github.com/jomcgi/homelab/projects/sextant/pkg/codegen",
    visibility = ["//visibility:public"],
    deps = ["//projects/sextant/pkg/schema"],
)

go_test(
    name = "codegen_test",
    srcs = [
        "builddata_test.go",
        "generator_output_files_test.go",
        "generator_spec_options_test.go",
        "generator_helpers_test.go",
        "generator_new_test.go",
        "generator_test.go",
    ],
    embed = [":codegen"],
    deps = ["//projects/sextant/pkg/schema"],
)

go_test(
    name = "codegen_internal_test",
    srcs = ["generator_helpers_test.go"],
    embed = [":codegen"],
    deps = ["//projects/sextant/pkg/schema"],
)
