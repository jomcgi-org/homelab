load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:python_generation_mode file

py_test(
    name = "staged_test",
    srcs = ["staged_test.py"],
    data = glob(["*.yaml"]) + [
        "@multitool//tools/kustomize",
    ],
    env = {
        "KUSTOMIZE_BIN": "$(rootpath @multitool//tools/kustomize)",
    },
    deps = ["@pip//pyyaml"],
)
