load("//bazel/tools/oci:go_image.bzl", "go_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The replay reads both databases through the hub's ships package.
# gazelle:resolve py ships //projects/monolith:pkg_ships

go_image(
    name = "image",
    binary = "//projects/ships/cmd",
)

filegroup(
    name = "repo_docs",
    srcs = ["ARCHITECTURE.md"],
    visibility = ["//:__pkg__"],
)

py_test(
    name = "replay_test",
    size = "large",
    timeout = "long",
    srcs = ["replay_test.py"],
    data = [
        "//projects/monolith/chart:migrations",
        "//projects/ships/chart:sql/bootstrap.sql",
        "//projects/ships/cmd",
        "@postgres_test//:postgres",
    ],
    env = {
        "PYTEST_ADDOPTS": "-p shared.testing.plugin",
        "SHIPS_BINARY": "$(rootpath //projects/ships/cmd)",
        "SHIPS_BOOTSTRAP_SQL": "$(rootpath //projects/ships/chart:sql/bootstrap.sql)",
    },
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//projects/monolith:pkg_ships",
        "//projects/monolith:shared_testing",  # keep
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//websockets",
    ],
)

py_test(
    name = "publish_test",
    size = "medium",
    srcs = ["publish_test.py"],
    data = [
        "//projects/monolith/chart:migrations",
        "//projects/ships/chart:sql/bootstrap.sql",
        "//projects/ships/cmd",
        "@postgres_test//:postgres",
    ],
    env = {
        "PYTEST_ADDOPTS": "-p shared.testing.plugin",
        "SHIPS_BINARY": "$(rootpath //projects/ships/cmd)",
        "SHIPS_BOOTSTRAP_SQL": "$(rootpath //projects/ships/chart:sql/bootstrap.sql)",
    },
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//projects/monolith:pkg_ships",
        "//projects/monolith:shared_testing",  # keep
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)
