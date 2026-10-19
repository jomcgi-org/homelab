# gazelle:ignore
load("@aspect_rules_py//py:defs.bzl", "py_test")

py_test(
    name = "publish_test",
    srcs = [
        "publish.py",
        "publish_test.py",
    ],
    imports = ["."],
    main = "publish_test.py",
)

py_test(
    name = "suite_test",
    srcs = [
        "publish.py",
        "suite.py",
        "suite_test.py",
    ],
    imports = ["."],
    main = "suite_test.py",
)
