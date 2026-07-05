# Hand-managed targets (gazelle would generate a package-named py_library
# with a different shape; same exclude convention as other hand-registered
# python packages in this repo). The generator itself is invoked as plain
# python3 from run-generators.sh, not as a bazel binary: py_venv_binary
# tools cannot run under genrule on RBE, so the output is committed instead.
# gazelle:exclude gen_env_readme.py
# gazelle:exclude gen_env_readme_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "gen_env_readme_lib",
    srcs = ["gen_env_readme.py"],
    imports = ["."],
)

py_test(
    name = "gen_env_readme_test",
    srcs = ["gen_env_readme_test.py"],
    deps = [
        ":gen_env_readme_lib",
        "@pip//pytest",
    ],
)
