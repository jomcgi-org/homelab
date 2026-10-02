load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "guestmem",
    srcs = [
        "client.go",
        "poller.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/guestmem",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = [
        "//projects/embervm/noded/guestagent",
        "//projects/embervm/noded/vsockproto",
    ],
)

go_test(
    name = "guestmem_test",
    srcs = [
        "client_test.go",
        "poller_test.go",
    ],
    embed = [":guestmem"],
    deps = ["//projects/embervm/noded/guestagent"],
)
