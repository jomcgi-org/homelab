load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "keys_test",
    srcs = ["keys_test.py"],
    data = [
        "BUILD",
        "agents/secrets.yaml",
        "kustomization.yaml",
        "recovery/reembed-local.yaml",
        "//projects/platform-factory/hindsight/resources:keys_resources",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

# Starts the deployed Hindsight image's server with resources/hindsight.yaml's
# settings and checks that the agents' MCP token reaches neither REST nor any
# tool but recall and retain. BuildBuddy runs it inside that image, as its
# non-root user (Postgres refuses root), with no network.
py_test(
    name = "boundary_test",
    size = "large",
    srcs = ["boundary_test.py"],
    data = ["//projects/platform-factory/hindsight/resources:keys_resources"],
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

# Stores memories with 1,024-dimensional vectors from a local stub, runs
# `hindsight-admin reembed` to the image's local model, and recalls them from a
# server on that model: the way back from OpenRouter embeddings (HOME-176).
# Same image and runner as boundary_test, with no network.
py_test(
    name = "rollback_test",
    size = "large",
    srcs = ["rollback_test.py"],
    exec_properties = {
        # Keep equal to resources/hindsight.yaml's image.
        "test.container-image": "docker://ghcr.io/vectorize-io/hindsight-api@sha256:662109ece429871a81e036427fb93fbb09ba9dc4b01d320d356f114c0e1aedd8",
        "test.nonroot-workspace": "true",
        "test.EstimatedMemory": "4GB",
        "test.EstimatedCPU": "2",
    },
    deps = ["@pip//pytest"],
)
