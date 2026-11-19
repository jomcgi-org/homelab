load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "restore_test",
    srcs = ["restore_test.sh"],
    args = ["$(location :restore.sh)"],
    data = ["restore.sh"],
)

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
