load("@aspect_rules_py//py:defs.bzl", "py_binary")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_binary(
    name = "measure_chunks",
    srcs = ["measure_chunks.py"],
    main = "measure_chunks.py",
    visibility = ["//visibility:public"],
)

py_test(
    name = "measure_chunks_test",
    srcs = [
        "measure_chunks.py",
        "measure_chunks_test.py",
    ],
    deps = ["@pip//pytest"],
)
