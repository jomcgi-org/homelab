load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "schema",
    srcs = [
        "reserved.go",
        "types.go",
        "validate.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/sextant/pkg/schema",
    visibility = ["//visibility:public"],
    deps = ["@in_gopkg_yaml_v3//:yaml_v3"],
)

go_test(
    name = "schema_test",
    srcs = [
        "reserved_test.go",
        "types_test.go",
        "validate_extra_test.go",
        "validate_fileio_test.go",
        "validate_internal_test.go",
        "validate_new_test.go",
        "validate_reserved_names_test.go",
        "validate_structural_test.go",
        "validate_test.go",
    ],
    embed = [":schema"],
    deps = ["@in_gopkg_yaml_v3//:yaml_v3"],
)
