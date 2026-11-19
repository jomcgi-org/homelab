load("@rules_shell//shell:sh_test.bzl", "sh_test")

# Semgrep mandoline from the private image ghcr.io/jomcgi/mandoline.
# Only private images may use these targets: never add them to a public image.
# The image holds one layer with /usr/local/bin/mandoline.
# gazelle:ignore

# The layer digest must match the image digest pinned in MODULE.bazel.
_LAYER = "blobs/sha256/66b6393de1bb8775c42540abb88e1068b8403921f71c141d23133dff92c245f1"

# Layer for oci_image(tars = [...]) in a private image.
genrule(
    name = "layer",
    srcs = ["@mandoline_linux_amd64"],
    outs = ["mandoline_layer.tar.gz"],
    cmd = "cp $(execpath @mandoline_linux_amd64)/%s $@" % _LAYER,
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
