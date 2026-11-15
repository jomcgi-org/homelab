# Hand-maintained snapshot contract, fixtures and their conformance test.
# gazelle:ignore
load("//bazel/tools/pytest:defs.bzl", "py_test")

filegroup(
    name = "schema",
    srcs = glob(["schema/**"]),
    visibility = ["//projects/factory-storyboard:__subpackages__"],
)

filegroup(
    name = "fixtures",
    srcs = glob(["fixtures/**/*.json"]),
    visibility = ["//projects/factory-storyboard:__subpackages__"],
)

py_test(
    name = "schema_test",
    srcs = ["schema_test.py"],
    data = [
        ":fixtures",
        ":schema",
    ],
    deps = [
        "@pip//jsonschema",
        "@pip//pytest",
    ],
)
