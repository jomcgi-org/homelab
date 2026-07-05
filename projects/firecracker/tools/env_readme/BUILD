load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")

py_venv_binary(
    name = "env_readme",
    srcs = ["gen_env_readme.py"],
    main = "gen_env_readme.py",
    visibility = ["//projects/firecracker:__subpackages__"],
)
