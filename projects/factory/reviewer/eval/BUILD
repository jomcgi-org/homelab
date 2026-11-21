load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:exclude score.py

py_test(
    name = "score_test",
    srcs = ["score_test.py"],
    data = [
        "cases.json",
        "score.py",
    ],
)
