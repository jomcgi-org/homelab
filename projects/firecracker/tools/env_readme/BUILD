load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "gen_env_readme_lib",
    srcs = ["gen_env_readme.py"],
    imports = ["."],
)

py_venv_binary(
    name = "env_readme",
    srcs = ["gen_env_readme.py"],
    main = "gen_env_readme.py",
    visibility = ["//projects/firecracker:__subpackages__"],
)

py_test(
    name = "gen_env_readme_test",
    srcs = ["gen_env_readme_test.py"],
    deps = [
        ":gen_env_readme_lib",
        "@pip//pytest",
    ],
)
