load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:python_generation_mode file
# The script runs in a container, not under Bazel; the test imports it from
# its path (below) and reads the manifests through the glob.
# gazelle:exclude provision.py

py_test(
    name = "provision_test",
    srcs = ["provision_test.py"],
    data = glob([
        "*.yaml",
        "provision.py",
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
