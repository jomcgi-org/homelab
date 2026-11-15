# gazelle:ignore

load("//bazel/tools/pytest:defs.bzl", "py_test")

# Explicitly launched workstation scripts. Playwright is installed in the
# local environment documented here, and never added to production images.
filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)

# Pure gate accounting runs in ordinary PR CI. Playwright remains confined to
# the separately provisioned browser gate, outside every production image.
py_test(
    name = "rehearsal_report_test",
    srcs = [
        "rehearsal_report.py",
        "rehearsal_report_test.py",
    ],
    data = ["budgets.json"],
    imports = ["."],
    deps = ["@pip//pytest"],
)

py_test(
    name = "local_profile_test",
    srcs = [
        "app.py",
        "local_profile_test.py",
    ],
    imports = ["."],
    deps = [
        "//projects/monolith:pkg_core",
        "//projects/monolith:pkg_grimoire",
        "//projects/monolith:pkg_platform_auth",
        "@pip//cryptography",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pyjwt",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)
