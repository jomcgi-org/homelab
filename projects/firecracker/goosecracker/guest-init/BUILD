load("//bazel/tools/oci:go_image.bzl", "go_image")

# In-microVM wrapper / PID 1 (ADR 022) container image. The agent harness image
# layers this binary in as its entrypoint.
go_image(
    name = "image",
    binary = "//projects/firecracker/goosecracker/guest-init/cmd",
)
