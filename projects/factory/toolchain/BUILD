load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The installer is invoked by bootstrap.sh, not imported as a library.
# gazelle:exclude install.py

py_test(
    name = "install_test",
    srcs = ["install_test.py"],
    data = ["install.py"],
    deps = ["@pip//pytest"],
)

sh_test(
    name = "bootstrap_test",
    srcs = ["bootstrap_test.sh"],
    data = [
        "bootstrap.sh",
        "install.py",
    ],
)
