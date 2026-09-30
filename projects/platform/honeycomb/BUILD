load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")

# gazelle:exclude sync.py
# gazelle:exclude sync_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "sync_lib",
    srcs = ["sync.py"],
    data = glob(["triggers/*.yaml"]),
    imports = ["."],
    deps = ["@pip//pyyaml"],
)

# Dry run by default; pass --apply to write. Reads HONEYCOMB_CONFIG_KEY.
py_venv_binary(
    name = "sync",
    data = glob(["triggers/*.yaml"]),
    main = "sync.py",
)

py_test(
    name = "sync_test",
    srcs = ["sync_test.py"],
    deps = [
        ":sync_lib",
        "@pip//pytest",
    ],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
