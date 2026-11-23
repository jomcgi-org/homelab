load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:python_generation_mode file
# The staging script runs in a container, not under Bazel; the test reads it
# through the glob below.
# gazelle:exclude assets
# gazelle:exclude conformance

py_test(
    name = "gvisor_fork_test",
    srcs = ["gvisor_fork_test.py"],
    data = glob([
        "*.yaml",
        "assets/*",
        "conformance/*",
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
