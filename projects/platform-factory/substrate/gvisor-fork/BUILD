load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:python_generation_mode file
# The staging script runs in a container, not under Bazel; the test reads it
# through the glob below.
# gazelle:exclude assets
# gazelle:exclude conformance
# gazelle:exclude latency
# gazelle:exclude xgates

# The files ../resources renders through its "../gvisor-fork" resource.
filegroup(
    name = "kustomize_inputs",
    srcs = glob([
        "*.yaml",
        "assets/*",
        "conformance/*",
    ]),
    visibility = ["//projects/platform-factory/substrate/resources:__pkg__"],
)

py_test(
    name = "gvisor_fork_test",
    srcs = ["gvisor_fork_test.py"],
    data = glob([
        "*.yaml",
        "assets/*",
        "conformance/*",
        "latency/*",
    ]) + [
        "@multitool//tools/kustomize",
    ],
    env = {
        "KUSTOMIZE_BIN": "$(rootpath @multitool//tools/kustomize)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
