load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_push")
load("//bazel/tools/oci:providers.bzl", "oci_image_info")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The private Codex+mandoline Harness image (HOME-530, HOME-652): the pinned
# Codex harness plus Semgrep mandoline, shared by the reviewer and coders. Push
# it only to a private package. The chart values reviewer.harnessImage and
# repositories[].harnessImage select it; deploy values leave them empty until
# the mandoline runner (HOME-529), its turn-on (HOME-531, HOME-653) and the
# push guard (HOME-651) land.
# gazelle:ignore

oci_image(
    name = "image",
    base = "@codex_harness_linux_amd64",
    # Add the HOME-529 runner layer after mandoline.
    tars = ["//bazel/tools/mandoline:layer"],
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
# https://factory-plan.jomcgi.dev/factory/issue/HOME-652#comment-8646
# GHCR assigns private visibility on first publication.
oci_push(
    name = "image.push",
    image = ":image",
    remote_tags = "image_stamped_tags",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/harness/codex",
    visibility = ["//bazel/images:__pkg__"],
)

oci_image_info(
    name = "image.info",
    image = ":image",
    image_digest = ":image.digest",
    image_tags = "image_stamped.tags.txt",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/harness/codex",
    visibility = ["//visibility:public"],
)

py_test(
    name = "harness_image_test",
    srcs = ["harness_image_test.py"],
    data = [
        ":image",
        "//:MODULE.bazel",
        "//projects/factory/deploy:deploy_values",
        "@codex_harness_linux_amd64",
    ],
    env = {
        "BASE_LAYOUT": "$(rootpath @codex_harness_linux_amd64)",
        "DEPLOY_VALUES": "$(rootpath //projects/factory/deploy:deploy_values)",
        "IMAGE_LAYOUT": "$(rootpath :image)",
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
