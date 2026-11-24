load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:python_generation_mode file

# The files ../../../resources renders through its "../egress-scan/gate/manifests"
# resource.
filegroup(
    name = "kustomize_inputs",
    srcs = glob(["*.yaml"]),
    visibility = ["//projects/platform-factory/substrate/resources:__pkg__"],
)

py_test(
    name = "manifests_test",
    srcs = ["manifests_test.py"],
    data = glob(["*.yaml"]) + [
        "//projects/platform-factory/substrate/egress-scan/gate/guard:kustomize_inputs",
        "@multitool//tools/kustomize",
    ],
    env = {
        "KUSTOMIZE_BIN": "$(rootpath @multitool//tools/kustomize)",
    },
    deps = ["@pip//pyyaml"],
)
