load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "kube_access_test",
    srcs = ["kube_access_test.py"],
    data = [
        "admins.yaml",
        "explorers.yaml",
        "flowcontrol.yaml",
        "kustomization.yaml",
        "sessions.yaml",
        "viewers.yaml",
        "//projects:platform-factory/kube-access/check-session-access.sh",
        "//projects/factory-cluster/nodes:authentication-config.yaml",
        "//projects/factory-cluster/nodes:factory-session-kubeconfig.yaml",
        "//projects/factory-cluster/nodes:factory-session-token.py",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
