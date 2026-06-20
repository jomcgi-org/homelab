load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:resolve py main //projects/trips/tools/publish-trip-images:publish-trip-images

py_library(
    name = "publish-trip-images",
    srcs = [
        "__init__.py",
        "main.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//httpx",
        "@pip//pillow",
        "@pip//rich",
        "@pip//typer",
    ],
)

py_venv_binary(
    name = "main",
    srcs = ["main.py"],
    main = "main.py",
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//httpx",
        "@pip//pillow",
        "@pip//rich",
        "@pip//typer",
    ],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_target_test(
    name = "main_semgrep_test",
    exclude_rules = [
        # TODO: fix naive datetime usage in publish-trip-images/main.py
        "datetime-now-naive-fallback",  # keep
    ],
    lockfiles = ["//bazel/requirements:all.txt"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    sca_rules = ["//bazel/semgrep/rules:sca_python_rules"],
    target = ":main",
)

# gazelle:resolve py publish_images_test //projects/trips/tools/publish-trip-images:publish-trip-images
py_test(
    name = "publish_images_test",
    srcs = ["publish_images_test.py"],
    imports = ["."],
    deps = [
        "//projects/trips/tools/publish-trip-images",
        "@pip//pillow",
        "@pip//pytest",
    ],
)

semgrep_test(
    name = "publish_images_test_semgrep_test",
    srcs = ["publish_images_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

# gazelle:resolve py main_test //projects/trips/tools/publish-trip-images:publish-trip-images
py_test(
    name = "main_test",
    srcs = ["main_test.py"],
    imports = ["."],
    deps = [
        "//projects/trips/tools/publish-trip-images",
        "@pip//pytest",
    ],
)

semgrep_test(
    name = "main_test_semgrep_test",
    srcs = ["main_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

# gazelle:resolve py ingest_post_test //projects/trips/tools/publish-trip-images:publish-trip-images
py_test(
    name = "ingest_post_test",
    srcs = ["ingest_post_test.py"],
    imports = ["."],
    deps = [
        "//projects/trips/tools/publish-trip-images",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

semgrep_test(
    name = "ingest_post_test_semgrep_test",
    srcs = ["ingest_post_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
