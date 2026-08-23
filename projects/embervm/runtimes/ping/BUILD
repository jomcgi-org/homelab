# gazelle:ignore
load("@rules_go//go:def.bzl", "go_binary")
load("//bazel/tools/oci:go_image.bzl", "go_image")

# Minimal serving-class prover. The pure Go binary is the guest's PID 1 and
# listens directly on the tap-facing HTTP port. go_image publishes the same
# :image and public :image.info targets that helm_chart consumes for the pi
# runtime, so the chart can inject a content-addressed guest image reference.
go_binary(
    name = "ping",
    srcs = ["main.go"],
)

go_image(
    name = "image",
    binary = ":ping",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/ping",
)
