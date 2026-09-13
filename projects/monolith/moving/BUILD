# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude tests

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

# Recursive sources and reviewed runtime dependencies are package-owned.
# keep
py_library(
    name = "moving",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-backend"],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "//projects/monolith/chat_public",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "//projects/monolith/shared",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pydantic",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_models_test",
    srcs = [
        "tests/__init__.py",
        "tests/conftest.py",
        "tests/models_test.py",
    ],
    data = ["//projects/monolith/chart:migrations"],
    imports = [".."],
    deps = [
        ":moving",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_collisions_test",
    srcs = [
        "tests/__init__.py",
        "tests/collisions_test.py",
    ],
    imports = [".."],
    deps = [
        ":moving",
        "@pip//pytest",
    ],
)

py_test(
    name = "moving_viewer_test",
    srcs = [
        "tests/__init__.py",
        "tests/conftest.py",
        "tests/viewer_test.py",
    ],
    imports = [".."],
    deps = [
        ":moving",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_router_test",
    srcs = [
        "tests/__init__.py",
        "tests/conftest.py",
        "tests/router_test.py",
    ],
    imports = [".."],
    deps = [
        ":moving",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_chat_test",
    srcs = [
        "tests/__init__.py",
        "tests/chat_test.py",
        "tests/conftest.py",
    ],
    imports = [".."],
    deps = [
        ":moving",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_gcal_sync_test",
    srcs = [
        "tests/__init__.py",
        "tests/conftest.py",
        "tests/gcal_sync_test.py",
    ],
    imports = [".."],
    deps = [
        ":moving",
        "//projects/monolith/core",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
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
