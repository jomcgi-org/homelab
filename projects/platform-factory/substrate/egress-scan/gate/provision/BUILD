load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:python_generation_mode file
# The script runs in a container, not under Bazel; the test imports it from
# its path (below) and reads the manifests through the glob.
# gazelle:exclude provision.py

# The files ../../../resources renders through its "../egress-scan/gate/provision"
# resource.
filegroup(
    name = "kustomize_inputs",
    srcs = glob([
        "*.yaml",
        "provision.py",
        "template.json",
    ]),
    visibility = ["//projects/platform-factory/substrate/resources:__pkg__"],
)

py_test(
    name = "provision_test",
    srcs = ["provision_test.py"],
    data = glob([
        "*.yaml",
        "provision.py",
        "template.json",
    ]) + [
        "@multitool//tools/kustomize",
    ],
    env = {
        "KUSTOMIZE_BIN": "$(rootpath @multitool//tools/kustomize)",
    },
    deps = ["@pip//pyyaml"],
)
