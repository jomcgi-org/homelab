load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:ignore

# Writes web/hub/src/lib/public/posts/posts-manifest.json from the
# `public: true` posts in docs/posts. bazel/tools/format/run-generators.sh
# runs it in the Format stage.
py_library(
    name = "posts",
    srcs = [
        "gen_posts_manifest.py",
        "prettier_json.py",
        "public_content.py",
    ],
    imports = ["."],
)

py_venv_binary(
    name = "gen_posts_manifest",
    srcs = ["gen_posts_manifest.py"],
    main = "gen_posts_manifest.py",
    deps = [":posts"],
)

py_test(
    name = "gen_posts_manifest_test",
    srcs = ["gen_posts_manifest_test.py"],
    deps = [
        ":posts",
        "@pip//pytest",
    ],
)

py_test(
    name = "prettier_json_test",
    srcs = ["prettier_json_test.py"],
    deps = [
        ":posts",
        "@pip//pytest",
    ],
)

# The PR content scan in buildbuddy.yaml imports public_content.py.
exports_files(
    ["public_content.py"],
    visibility = ["//bazel/tools/ci:__pkg__"],
)
