# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# The explicit preview-layout target owns this executable entrypoint.
# gazelle:exclude preview-layout.py

load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_test(
    name = "scripts_preview_layout_test",
    srcs = ["preview_layout_test.py"],
    data = ["preview-layout.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//networkx",
        "@pip//pytest",
    ],
)

filegroup(
    name = "production_sources",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "non_test_python_sources",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "**/conftest.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
