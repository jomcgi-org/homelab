# Hand-managed targets: gazelle's package-named py_library would collide
# with the py_venv_binary below (same exclude convention as other
# hand-registered python packages in this repo).
# gazelle:exclude gen_env_readme.py
# gazelle:exclude gen_env_readme_test.py

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
