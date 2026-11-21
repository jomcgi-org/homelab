load("//bazel/tools/pytest:defs.bzl", "py_test")

# The test needs installer and unit files as runfiles.
# gazelle:exclude install_t3_socket_test.py
# The node installer executes this host helper outside Bazel.
# gazelle:exclude factory-session-token.py

exports_files(
    [
        "authentication-config.yaml",
        "factory-session-kubeconfig.yaml",
        "factory-session-token.py",
    ],
    visibility = ["//projects/platform-factory/kube-access/resources:__pkg__"],
)

py_test(
    name = "install_t3_socket_test",
    srcs = ["install_t3_socket_test.py"],
    data = [
        "install-t3-socket.sh",
        "t3-cluster.service",
        "t3-cluster.socket",
    ],
    deps = ["@pip//pytest"],
)
