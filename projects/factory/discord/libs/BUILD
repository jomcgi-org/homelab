load("//bazel/tools/pytest:defs.bzl", "py_test")
load(":libs.bzl", "LIBS")

exports_files(
    [
        "house/house-1.css",
        "house/house-1.js",
        "selftest/architecture.html",
    ],
    visibility = ["//projects/factory/connector/image:__pkg__"],
)

PAGES = [
    "//projects/factory/discord/libs:selftest/architecture.html",
    "//projects/factory/discord/artifacts:template.html",
    "//projects/factory/discord/artifacts:examples/comparison.html",
    "//projects/factory/discord/artifacts:examples/explainer.html",
    "//projects/factory/discord/artifacts:examples/report.html",
]

# Every /_lib/ reference in the pages carries the sha384 of the shipped file.
py_test(
    name = "sri_test",
    srcs = ["sri_test.py"],
    # The self-test page is in both lists.
    data = PAGES[1:] + LIBS.values(),
    env = {
        "LIB_FILES": " ".join(["$(rootpath %s)" % label for label in LIBS.values()]),
        "LIB_NAMES": " ".join(LIBS.keys()),
        "PAGES": " ".join(["$(rootpath %s)" % page for page in PAGES]),
    },
)
