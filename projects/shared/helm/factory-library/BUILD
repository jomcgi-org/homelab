load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Tests write fake_rclone.py as an executable named rclone; nothing imports it.
# gazelle:exclude fake_rclone.py

exports_files(
    ["fake_rclone.py"],
    visibility = ["//projects/factory-public/chart:__pkg__"],
)

# Expose this package's README to the repository documentation aggregate.
filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//:__pkg__"],
)

py_test(
    name = "library_test",
    srcs = ["library_test.py"],
    data = [
        "README.md",
        "fake_rclone.py",
        "//projects/shared/helm/factory-library/chart",
        "@multitool//tools/helm",
    ] + glob(["goldens/**"]),
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

# A review helper run by hand (see README). Gazelle generates this target for
# the script's __main__ block; py_venv_binary needs main set explicitly.
py_venv_binary(
    name = "render_landing_diff",
    srcs = ["render_landing_diff.py"],
    main = "render_landing_diff.py",
    visibility = ["//:__subpackages__"],
    deps = ["@pip//pyyaml"],
)

py_library(
    name = "factory-library",
    srcs = ["render_landing_diff.py"],
    visibility = ["//:__subpackages__"],
    deps = ["@pip//pyyaml"],
)
