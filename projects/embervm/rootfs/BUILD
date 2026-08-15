# Hand-managed targets. The repo maps py_binary to py_venv_binary
# (`# gazelle:map_kind` in //:BUILD), so a generated entry point would collide
# with any hand-written binary of the same name, and gazelle cannot resolve the
# sibling `import measure_chunks` from the test to a target it did not create.
# Both sources are excluded and the targets are written by hand, the same
# convention as //projects/firecracker/tools/env_readme. The harness itself is
# invoked as plain python3 against real images on a brick, not through bazel.
# gazelle:exclude measure_chunks.py
# gazelle:exclude measure_chunks_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "measure_chunks_lib",
    srcs = ["measure_chunks.py"],
    imports = ["."],
    visibility = ["//visibility:public"],
)

py_test(
    name = "measure_chunks_test",
    srcs = ["measure_chunks_test.py"],
    deps = [
        ":measure_chunks_lib",
        "@pip//pytest",
    ],
)
