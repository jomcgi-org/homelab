load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

# Semgrep mandoline from the private image ghcr.io/jomcgi/mandoline.
# Only private images may use these targets: never add them to a public image.
# The image holds one layer with /usr/local/bin/mandoline.
# gazelle:ignore

# Layer for oci_image(tars = [...]) in a private image. MODULE.bazel pins it.
genrule(
    name = "layer",
    srcs = ["@mandoline_layer//:layer.tar.gz"],
    outs = ["mandoline_layer.tar.gz"],
    cmd = "cp $< $@",
    visibility = ["//projects/factory:__subpackages__"],
)

genrule(
    name = "mandoline",
    srcs = [":layer"],
    outs = ["bin/mandoline"],
    cmd = "tar -xzf $< -O usr/local/bin/mandoline > $@ && chmod 0755 $@",
    executable = True,
    visibility = ["//projects/factory:__subpackages__"],
)

sh_test(
    name = "mandoline_test",
    srcs = ["mandoline_test.sh"],
    args = ["$(rootpath :mandoline)"],
    data = [":mandoline"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

bzl_library(
    name = "private_layer",
    srcs = ["private_layer.bzl"],
    visibility = ["//visibility:private"],
)
