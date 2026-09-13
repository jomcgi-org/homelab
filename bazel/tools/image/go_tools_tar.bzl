"""Package source-built Go tools into per-platform tar layers."""

load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")

_PLATFORMS = {
    "linux_amd64": "@rules_go//go/toolchain:linux_amd64",
    "linux_arm64": "@rules_go//go/toolchain:linux_arm64",
    "darwin_arm64": "@rules_go//go/toolchain:darwin_arm64",
}

def go_tools_tar(name, tools, package_dir = "/usr/bin", visibility = None):
    """Build Go binaries for each tools-image platform and put them in a tar.

    Args:
        name: Base name for generated targets.
        tools: Dict from installed command name to a go_binary label.
        package_dir: Image directory that receives the commands.
        visibility: Visibility of generated platform targets.
    """
    for platform, target_platform in _PLATFORMS.items():
        layer_name = name + "_untransitioned_" + platform
        tar(
            name = layer_name,
            srcs = tools.values(),
            tags = ["manual"],
            mtree = [
                "./{package_dir}/{command} type=file mode=0755 content=$(execpath {target})".format(
                    package_dir = package_dir.lstrip("/"),
                    command = command,
                    target = target,
                )
                for command, target in tools.items()
            ],
        )
        platform_transition_filegroup(
            name = name + "_" + platform,
            srcs = [":" + layer_name],
            target_platform = target_platform,
            visibility = visibility,
        )
