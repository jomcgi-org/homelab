# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# embervm-noded daemon container image. Built as an apko image (Wolfi base) rather
# than a distroless go_image because it drives Firecracker guests as child
# processes, mirroring the fc-invoke daemon it forks. DUAL-ARCH, exactly like the
# fc-invoke daemon image: the Go binary is cross-compiled per arch and layered at
# /usr/local/bin/embervm-noded (the apko entrypoint), and @kata_firecracker bakes
# firecracker + the guest kernel at /opt/fc so the node needs no /opt/kata install.
# node-4 (amd64) is the only Firecracker node today so the aarch64 manifest is
# unused, but building it keeps this on apko's proven multiarch_tars path (the
# amd64-only + multiarch_tars combination is an untested apko_image code path that
# fails to push a layer blob). noded carries no arch-specific artifact, so dual-
# arch is free.

platform_transition_filegroup(
    name = "noded_amd64_bin",
    srcs = ["//projects/embervm/noded/cmd:noded"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "noded_tar_amd64",
    srcs = [":noded_amd64_bin"],
    mtree = [
        "./usr/local/bin/embervm-noded type=file content=$(execpath :noded_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

platform_transition_filegroup(
    name = "noded_arm64_bin",
    srcs = ["//projects/embervm/noded/cmd:noded"],
    target_platform = "@rules_go//go/toolchain:linux_arm64",
)

tar(
    name = "noded_tar_arm64",
    srcs = [":noded_arm64_bin"],
    mtree = [
        "./usr/local/bin/embervm-noded type=file content=$(execpath :noded_arm64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@embervm_noded_lock//:contents",
    # :noded_tar is the Go binary (multiarch_tars appends _amd64/_arm64 per arch);
    # @kata_firecracker bakes the firecracker binary + guest kernel into /opt/fc.
    multiarch_tars = [
        ":noded_tar",
        "@kata_firecracker//:tar",
    ],
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/noded/image",
)
