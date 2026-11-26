load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_push")
load("//bazel/tools/oci:providers.bzl", "oci_image_info")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The Hermes Agent harness image (HOME-906): the pinned Hermes image plus the
# kagent-hermes adapter, which serves A2A and calls Hermes' API server on
# loopback. No chart or deploy value uses it yet (HOME-905 slice 2).
# gazelle:ignore

oci_image(
    name = "image",
    base = "@hermes_agent_linux_amd64",
    # The adapter replaces the image's s6 entrypoint and starts
    # `hermes gateway` itself.
    cmd = [],
    entrypoint = ["/usr/local/bin/kagent-hermes"],
    env = {
        "HERMES_HOME": "/data/hermes",
        "HOME": "/data",
    },
    tars = ["//projects/factory/harness/hermes/cmd/kagent-hermes:layer"],
    user = "65532:65532",
    workdir = "/data",
)

expand_template(
    name = "image_stamped_tags",
    out = "image_stamped.tags.txt",
    stamp_substitutions = {
        "{STABLE_IMAGE_TAG}": "{{STABLE_IMAGE_TAG}}",
    },
    template = ["{STABLE_IMAGE_TAG}"],
)

# GHCR assigns private visibility on first publication.
oci_push(
    name = "image.push",
    image = ":image",
    remote_tags = "image_stamped_tags",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/harness/hermes",
    visibility = ["//bazel/images:__pkg__"],
)

oci_image_info(
    name = "image.info",
    image = ":image",
    image_digest = ":image.digest",
    image_tags = "image_stamped.tags.txt",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/harness/hermes",
    visibility = ["//visibility:public"],
)

# Runs the built image's adapter and Hermes against a fake model on loopback.
py_test(
    name = "harness_image_test",
    size = "large",
    srcs = ["harness_image_test.py"],
    data = [
        ":image",
        "@hermes_agent_linux_amd64",
    ],
    env = {
        "BASE_LAYOUT": "$(rootpath @hermes_agent_linux_amd64)",
        "IMAGE_LAYOUT": "$(rootpath :image)",
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
