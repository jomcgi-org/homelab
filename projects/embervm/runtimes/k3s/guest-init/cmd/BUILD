load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

# ember-k3s-init: PID 1 of the EmberVM composite (R5) k3s guest microVMs (ADR
# embervm/001, plan Task 3). ONE init binary serves both the k3s-server and
# k3s-agent images; the role is data (EMBER_GROUP_ROLE), not baked per image. On
# a raw Firecracker boot (init=<HarnessInit>, OCI entrypoint ignored) it mounts
# k3s's writable + pseudo filesystems, decodes the MMDS-lite EMBER_GROUP_* facts,
# starts the vsock guest control agent (guestagent) sidecar on the frozen port
# 1024 for post-resume clock resync (standing decision 7), maps the facts to k3s
# flags (standing decision 13), and supervises `k3s server`/`k3s agent`. The
# baked airgap tarball is imported by k3s itself (zero-egress, decision 12).
# Cross-compiled per arch and layered into the k3s images at
# /usr/local/bin/ember-k3s-init by ../../{k3s-server,k3s-agent}/BUILD.
go_library(
    name = "cmd_lib",
    srcs = [
        "agent_linux.go",
        "agent_other.go",
        "cmdline.go",
        "k3s.go",
        "main.go",
        "mount_linux.go",
        "mount_other.go",
        "ready_linux.go",
        "ready_other.go",
        "supervise.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/k3s/guest-init/cmd",
    visibility = ["//visibility:private"],
    deps = select({
        "@rules_go//go/platform:android": [
            "//projects/embervm/noded/guestagent",
            "//projects/firecracker/substrate/shim",
            "//projects/firecracker/substrate/vsockproto",
            "@org_golang_x_sys//unix",
        ],
        "@rules_go//go/platform:linux": [
            "//projects/embervm/noded/guestagent",
            "//projects/firecracker/substrate/shim",
            "//projects/firecracker/substrate/vsockproto",
            "@org_golang_x_sys//unix",
        ],
        "//conditions:default": [],
    }),
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = [
        "cmdline_test.go",
        "k3s_test.go",
    ],
    embed = [":cmd_lib"],
)
