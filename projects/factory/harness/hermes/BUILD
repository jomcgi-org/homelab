load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("@aspect_bazel_lib//lib:run_binary.bzl", "run_binary")
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_rules_py//py:defs.bzl", "py_binary")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_push")
load("//bazel/tools/oci:providers.bzl", "oci_image_info")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The Hermes Agent harness image (HOME-906, HOME-907): a slim copy of the
# pinned Hermes image plus the kagent fork's kagent-hermes runner, which
# serves kagent's private A2A and calls Hermes' API server on loopback.
# gazelle:ignore

py_binary(
    name = "slim",
    srcs = ["slim.py"],
)

# The upstream Debian layer, Hermes with its Python and virtualenv, and the
# fork's runner layer. The browser, FFmpeg, Node.js, the C toolchain and the
# UIs stay out (slim.py).
run_binary(
    name = "slim_layers",
    srcs = [
        "@hermes_agent_linux_amd64",
        "@hermes_runner_linux_amd64",
    ],
    outs = [
        "base.tar.gz",
        "hermes.tar.gz",
        "runner.tar.gz",
    ],
    args = [
        "--layout",
        "$(execpath @hermes_agent_linux_amd64)",
        "--runner-layout",
        "$(execpath @hermes_runner_linux_amd64)",
        "--base",
        "$(execpath base.tar.gz)",
        "--hermes",
        "$(execpath hermes.tar.gz)",
        "--runner",
        "$(execpath runner.tar.gz)",
    ],
    tool = ":slim",
)

# The durable /data directory the runner writes HERMES_HOME into.
tar(
    name = "data_layer",
    mtree = ["./data type=dir mode=0755 uid=65532 gid=65532"],
)

oci_image(
    name = "image",
    architecture = "amd64",
    entrypoint = ["/usr/local/bin/kagent-hermes"],
    env = {
        "HERMES_HOME": "/data/hermes",
        "HOME": "/data",
        "PATH": "/opt/hermes/bin:/opt/hermes/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    },
    os = "linux",
    tars = [
        "base.tar.gz",
        "hermes.tar.gz",
        "runner.tar.gz",
        ":data_layer",
    ],
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

# Checks the slim image's layers and runner, and runs its Hermes gateway
# against a fake model on loopback.
py_test(
    name = "harness_image_test",
    size = "large",
    srcs = ["harness_image_test.py"],
    data = [
        ":image",
        "@hermes_agent_linux_amd64",
        "@hermes_runner_linux_amd64",
    ],
    env = {
        "BASE_LAYOUT": "$(rootpath @hermes_agent_linux_amd64)",
        "IMAGE_LAYOUT": "$(rootpath :image)",
        "RUNNER_LAYOUT": "$(rootpath @hermes_runner_linux_amd64)",
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
