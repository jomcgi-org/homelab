load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_push")
load("//bazel/tools/oci:providers.bzl", "oci_image_info")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The private Claude+mandoline Harness image (HOME-684): the pinned Claude
# harness plus Semgrep mandoline, for the INTSEC audit binding (HOME-235). Push
# it only to a private package. Codex agents use ../codex (HOME-652).
# gazelle:ignore

oci_image(
    name = "image",
    base = "@claude_harness_linux_amd64",
    tars = [
        "//bazel/tools/mandoline:layer",
        "//projects/factory/mandoline/cmd/factory-mandoline:audit_layer",
    ],
)

expand_template(
    name = "image_stamped_tags",
    out = "image_stamped.tags.txt",
    stamp_substitutions = {
        "{STABLE_IMAGE_TAG}": "{{STABLE_IMAGE_TAG}}",
    },
    template = ["{STABLE_IMAGE_TAG}"],
)

# The package owner verified this destination was absent and approved its
# first private publication.
# https://github.com/jomcgi-org/homelab/pull/8454#issuecomment-7055813923
# GHCR assigns private visibility on first publication.
oci_push(
    name = "image.push",
    image = ":image",
    remote_tags = "image_stamped_tags",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/harness/claude",
    visibility = ["//bazel/images:__pkg__"],
)

oci_image_info(
    name = "image.info",
    image = ":image",
    image_digest = ":image.digest",
    image_tags = "image_stamped.tags.txt",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/harness/claude",
    visibility = ["//visibility:public"],
)

py_test(
    name = "harness_image_test",
    srcs = ["harness_image_test.py"],
    data = [
        ":image",
        "//:MODULE.bazel",
        "//projects/factory/chart:kagent_chart",
        "//projects/factory/deploy:deploy_values",
        "@claude_harness_linux_amd64",
    ],
    env = {
        "BASE_LAYOUT": "$(rootpath @claude_harness_linux_amd64)",
        "DEPLOY_VALUES": "$(rootpath //projects/factory/deploy:deploy_values)",
        "IMAGE_LAYOUT": "$(rootpath :image)",
        "KAGENT_CHART": "$(rootpath //projects/factory/chart:kagent_chart)",
        "MODULE_BAZEL": "$(rootpath //:MODULE.bazel)",
    },
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
