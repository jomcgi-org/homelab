# gazelle:ignore
load("@rules_go//go:def.bzl", "go_binary", "go_library")
load("//bazel/tools/oci:go_image.bzl", "go_image")

# Minimal serving-class prover. One binary, two listeners: the guest answers
# BuildBase's HTTP-over-vsock readiness probe on vsockproto.GuestHTTPPort and
# serves live traffic on the tap-facing TCP port. go_image publishes the same
# :image and public :image.info targets that helm_chart consumes for the pi
# runtime, so the chart can inject a content-addressed guest image reference.
go_library(
    name = "ping_lib",
    srcs = [
        "main.go",
        "vsock_linux.go",
        "vsock_other.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/ping",
    visibility = ["//visibility:private"],
    deps = select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix",
            "//projects/firecracker/substrate/vsockproto",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix",
            "//projects/firecracker/substrate/vsockproto",
        ],
        "//conditions:default": [
            "//projects/firecracker/substrate/vsockproto",
        ],
    }),
)

go_binary(
    name = "ping",
    embed = [":ping_lib"],
)

go_image(
    name = "image",
    binary = ":ping",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/ping",
)
