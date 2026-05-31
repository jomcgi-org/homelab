# gazelle:semgrep_exclude_rules no-requests,boto3-endpoint-url-missing-scheme
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test", "semgrep_test")
load("//bazel/tools/oci:py3_image.bzl", "py3_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_venv_binary(
    name = "update",
    srcs = ["update.py"],
    data = ["//projects/hikes/scrape_walkhighlands:walks.db"],
    main = "update.py",
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//boto3",
        "@pip//botocore",
        "@pip//brotli",
        "@pip//pydantic",
        "@pip//python_dateutil",
        "@pip//requests",
    ],
)

py3_image(
    name = "update_image",
    binary = ":update",
)

py_library(
    name = "update_forecast",
    srcs = [
        "__init__.py",
        "update.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//boto3",
        "@pip//botocore",
        "@pip//brotli",
        "@pip//pydantic",
        "@pip//python_dateutil",
        "@pip//requests",
    ],
)

py_test(
    name = "update_test",
    srcs = ["update_test.py"],
    imports = ["."],
    deps = [
        ":update_forecast",
        "@pip//pytest",
        "@pip//requests",
    ],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    exclude_rules = ["no-requests"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "update_test_semgrep_test",
    srcs = ["update_test.py"],
    exclude_rules = ["no-requests"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_target_test(
    name = "update_semgrep_test",
    exclude_rules = ["no-requests"],
    lockfiles = ["//bazel/requirements:all.txt"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    sca_rules = ["//bazel/semgrep/rules:sca_python_rules"],
    target = ":update",
)
