load("@aspect_rules_js//js:defs.bzl", "js_library", "js_test")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("//bazel/tools/oci:py3_image.bzl", "py3_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Hand-maintained snapshot contract, fixtures and their conformance test.
# gazelle:ignore

# rules_js copies cross-package sources through their owning js_library.
js_library(
    name = "web_schema",
    srcs = glob(["schema/**"]),
    visibility = [
        "//projects/jomcgi-site/data:__pkg__",
        "//projects/jomcgi-site/web:__pkg__",
    ],
)

js_library(
    name = "web_fixtures",
    srcs = glob(["fixtures/**/*.json"]),
    visibility = [
        "//projects/jomcgi-site/check:__pkg__",
        "//projects/jomcgi-site/web:__pkg__",
    ],
)

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

js_test(
    name = "rehash_test",
    data = ["fixtures/rehash.mjs"],
    entry_point = "fixtures/rehash.test.mjs",
)

# Indexed through //:repo_docs so the factory does not modify the monolith.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//:__pkg__"],
)

py_test(
    name = "publisher_test",
    srcs = [
        "publisher.py",
        "publisher_test.py",
        "session_history.py",
    ],
    data = ["@gitleaks_linux_amd64//:gitleaks"],
    env = {"GITLEAKS_BIN": "$(rootpath @gitleaks_linux_amd64//:gitleaks)"},
    deps = ["@pip//pytest"],
)

py_test(
    name = "delivery_stats_test",
    srcs = [
        "delivery_stats.py",
        "delivery_stats_test.py",
    ],
    deps = ["@pip//pytest"],
)

py_venv_binary(
    name = "runtime",
    srcs = [
        "connector_source.py",
        "delivery_stats.py",
        "publisher.py",
        "runtime.py",
        "session_history.py",
        "task_reader.py",
    ],
    imports = ["."],
    main = "runtime.py",
    deps = [
        "@pip//boto3",
        "@pip//psycopg",
        "@pip//psycopg_binary",
    ],
)

pkg_tar(
    name = "scanner",
    srcs = ["@gitleaks_linux_amd64//:gitleaks"],
    mode = "0755",
    package_dir = "/usr/local/bin",
)

py3_image(
    name = "image",
    binary = ":runtime",
    env = {
        "PYTHONPATH": "/projects/factory-storyboard/runtime.runfiles/_main/projects/factory-storyboard",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    },
    tars = [":scanner"],
)

py_test(
    name = "runtime_test",
    srcs = [
        "connector_source.py",
        "delivery_stats.py",
        "publisher.py",
        "runtime.py",
        "runtime_test.py",
        "session_history.py",
        "task_reader.py",
    ],
    data = [":schema"],
    deps = [
        "@pip//jsonschema",
        "@pip//pytest",
    ],
)
