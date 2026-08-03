# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")
platform_transition_filegroup(name="tokenbroker_amd64_bin", srcs=["//projects/embervm/tokenbroker/cmd/tokenbroker:tokenbroker"], target_platform="@rules_go//go/toolchain:linux_amd64")
platform_transition_filegroup(name="tokenbroker_arm64_bin", srcs=["//projects/embervm/tokenbroker/cmd/tokenbroker:tokenbroker"], target_platform="@rules_go//go/toolchain:linux_arm64")
tar(name="tokenbroker_tar_amd64", srcs=[":tokenbroker_amd64_bin"], mtree=["./usr/local/bin/tokenbroker type=file content=$(execpath :tokenbroker_amd64_bin) mode=0755 uid=0 gid=0"])
tar(name="tokenbroker_tar_arm64", srcs=[":tokenbroker_arm64_bin"], mtree=["./usr/local/bin/tokenbroker type=file content=$(execpath :tokenbroker_arm64_bin) mode=0755 uid=0 gid=0"])
apko_image(name="image", config="apko.yaml", contents="@embervm_tokenbroker_lock//:contents", multiarch_tars=[":tokenbroker_tar"], repository="ghcr.io/jomcgi/homelab/projects/embervm/tokenbroker")
