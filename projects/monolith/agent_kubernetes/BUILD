# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

exports_files(
    [
        "client_test.py",
        "cluster_snapshot.py",
        "cluster_snapshot_test.py",
        "mcp_test.py",
    ],
    visibility = ["//projects/monolith:__pkg__"],
)

py_library(
    name = "agent_kubernetes",
    srcs = [
        "__init__.py",
        "client.py",
        "cluster_snapshot.py",
        "mcp.py",
    ],
    imports = [".."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = [
        "//projects/monolith:pkg_auth",
        "//projects/monolith:pkg_core",
        "//projects/monolith:pkg_shared",
        "@pip//kubernetes_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)
