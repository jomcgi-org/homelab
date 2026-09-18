# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

exports_files(
    [
        "client_test.py",
        "mcp_test.py",
    ],
    visibility = ["//projects/monolith:__pkg__"],
)

py_library(
    name = "agent_kubernetes",
    srcs = [
        "__init__.py",
        "client.py",
        "mcp.py",
    ],
    imports = [".."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = [
        "//projects/monolith:pkg_auth",
        "@pip//kubernetes_asyncio",
    ],
)
