load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:resolve py projects.factory.mandoline.cache //projects/factory/mandoline:mandoline
# gazelle:resolve py projects.factory.mandoline.execution //projects/factory/mandoline:mandoline

py_test(
    name = "execution_test",
    srcs = ["execution_test.py"],
    deps = [
        "//projects/factory/mandoline",
        "@pip//pytest",
    ],
)

py_test(
    name = "cache_test",
    srcs = ["cache_test.py"],
    deps = [
        "//projects/factory/mandoline",
        "@pip//pytest",
    ],
)

py_library(
    name = "mandoline",
    srcs = [
        "cache.py",
        "execution.py",
    ],
    visibility = ["//:__subpackages__"],
)
