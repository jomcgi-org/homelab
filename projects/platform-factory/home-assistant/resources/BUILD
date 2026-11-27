load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The image audit renders this directory with kustomize
# (//projects/platform-factory/image-audit:platform_images_test).
filegroup(
    name = "audit_manifests",
    srcs = glob(
        ["**"],
        exclude = [
            "BUILD",
            "BUILD.bazel",
        ],
    ),
    visibility = ["//projects/platform-factory/image-audit:__pkg__"],
)

py_library(
    name = "resources",
    srcs = ["install-auth-oidc.py"],
    visibility = ["//:__subpackages__"],
)

py_test(
    name = "backup_test",
    srcs = ["backup_test.py"],
    data = [
        "backup.yaml",
        "home-assistant.yaml",
        "kustomization.yaml",
        "zigbee2mqtt.yaml",
        "//projects/platform-factory/garage/resources:backup_resources",
        "//projects:platform-factory/home-assistant/application.yaml",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
