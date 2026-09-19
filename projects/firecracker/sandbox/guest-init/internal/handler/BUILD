# gazelle:exclude elixir_runtime_test.go
load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "handler",
    srcs = [
        "handler.go",
        "lang.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/sandbox/guest-init/internal/handler",
    visibility = ["//projects/firecracker/sandbox/guest-init:__subpackages__"],
    deps = [
        "//projects/firecracker/substrate/shim",
    ],
)

go_test(
    name = "handler_test",
    srcs = [
        "handler_test.go",
        "lang_test.go",
    ],
    embed = [":handler"],
    deps = [
        "//projects/firecracker/substrate/shim",
    ],
)

# keep: this Linux-only test runs against the lock-pinned sandbox APK payload.
go_test(
    name = "elixir_runtime_test",
    srcs = ["elixir_runtime_test.go"],
    data = ["@sandbox_elixir_lock//:contents"],
    embed = [":handler"],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = [
        "//projects/firecracker/substrate/shim",
    ],
)
