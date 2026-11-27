load("@aspect_rules_py//py:defs.bzl", "py_library")

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
