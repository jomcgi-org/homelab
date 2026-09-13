# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.

load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_test(
    name = "e2e_test",
    size = "large",
    timeout = "moderate",
    srcs = [
        "__init__.py",
        "conftest.py",
        "e2e_test.py",
    ],
    data = [
        "//projects/monolith/chart:migrations",
        "@postgres_test//:postgres",
    ],
    env = {"PYTEST_ADDOPTS": "-p shared.testing.plugin"},
    imports = [".."],
    tags = ["e2e"],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/shared:shared_testing",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//psycopg",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_test(
    name = "e2e_playwright_test",
    size = "large",
    timeout = "long",
    srcs = [
        "__init__.py",
        "conftest.py",
        "e2e_playwright_test.py",
    ],
    data = [
        "//projects/monolith:frontend_dist",
        "//projects/monolith/chart:migrations",
        "@postgres_test//:postgres",
    ],
    env = {"PYTEST_ADDOPTS": "-p shared.testing.plugin"},
    imports = [".."],
    tags = [
        "e2e",
        "manual",
        "playwright",
    ],
    deps = [
        "//projects/monolith/knowledge",
        "//projects/monolith/shared:shared_testing",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//psycopg",
        "@pip//pytest",
        # TODO: add @pip//pytest_playwright once requirements lock is regenerated
        "@pip//sqlmodel",
        "@pip//tzdata",
        "@pip//uvicorn",
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
