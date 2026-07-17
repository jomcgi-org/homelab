load("@rules_go//go:def.bzl", "go_library", "go_test")

# guestagent: the EmberVM composite (R5) guest control agent. A tiny vsock server
# baked into the k3s guest images that answers the frozen group-guest-agent
# contract on port 1024 (length-prefixed JSON frames, D-R2.6.1): its v1 command
# `sync_clock` sets CLOCK_REALTIME from the node-supplied host epoch after a
# snapshot resume and returns the post-set clock so the node verifies the delta
# (standing decision 7). The clock syscalls sit behind the Clock interface so the
# frame codec and handler are table-testable without root. Consumed by the k3s
# guest-init (runtimes/k3s/guest-init/cmd) as a supervised sidecar of PID 1.
go_library(
    name = "guestagent",
    srcs = [
        "agent.go",
        "clock_linux.go",
        "clock_other.go",
        "frame.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/guestagent",
    visibility = ["//visibility:public"],
    deps = select({
        "@rules_go//go/platform:android": [
            "@org_golang_x_sys//unix:go_default_library",
        ],
        "@rules_go//go/platform:linux": [
            "@org_golang_x_sys//unix:go_default_library",
        ],
        "//conditions:default": [],
    }),
)

go_test(
    name = "guestagent_test",
    srcs = [
        "agent_test.go",
        "frame_test.go",
    ],
    embed = [":guestagent"],
)
