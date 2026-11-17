load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "keys_test",
    srcs = ["keys_test.py"],
    data = [
        "BUILD",
        "application.yaml",
        "application-discord.yaml",
        "discord/network-policy.yaml",
        "discord/secrets.yaml",
        "//projects/platform-factory/hindsight-discord/resources:keys_resources",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

# Runs the deployed image with its manifest settings to check REST auth and
# disabled MCP. PR CI runs this as the image user, without external network.
py_test(
    name = "boundary_test",
    size = "large",
    srcs = ["boundary_test.py"],
    data = ["//projects/platform-factory/hindsight-discord/resources:keys_resources"],
    exec_properties = {
        # Keep equal to resources/hindsight.yaml's image.
        "test.container-image": "docker://ghcr.io/vectorize-io/hindsight-api@sha256:662109ece429871a81e036427fb93fbb09ba9dc4b01d320d356f114c0e1aedd8",
        "test.nonroot-workspace": "true",
        "test.EstimatedMemory": "4GB",
        "test.EstimatedCPU": "2",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
