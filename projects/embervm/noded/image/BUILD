# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# embervm-noded daemon container image. Built as an apko image (Wolfi base) rather
# than a distroless go_image because it drives Firecracker guests as child
# processes, mirroring the fc-invoke daemon it forks. DUAL-ARCH, exactly like the
# fc-invoke daemon image: the Go binary is cross-compiled per arch and layered at
# /usr/local/bin/embervm-noded (the apko entrypoint), and @kata_firecracker bakes
# firecracker, snapshot-editor, and the guest kernel at /opt/fc so the node needs
# no /opt/kata install.
# node-4 (amd64) is the only Firecracker node today, so the aarch64 manifest was
# never consumed and is no longer built.
#
# The note that used to live here warned that amd64-only + multiarch_tars is an
# untested apko path that FAILS TO PUSH A LAYER BLOB. That warning still stands,
# and it is why the tars below are named per arch explicitly: the broken
# combination is `arm64 = False` WITH multiarch_tars, not `arm64 = False` itself.
# The claude runtime has shipped on the explicit-tars shape for months. The
# failure is at push time, so PR CI cannot see it: watch the first main deploy.

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

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_noded_lock//:contents",
    # :noded_tar is the Go binary (multiarch_tars appends _amd64/_arm64 per arch);
    # @kata_firecracker bakes firecracker, snapshot-editor, and the guest kernel
    # into /opt/fc.
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/noded/image",
    tars = [
        ":noded_tar_amd64",
        "@kata_firecracker//:tar_amd64",
    ],
)
