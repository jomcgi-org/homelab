"""Package source-built Go tools into per-platform tar layers."""

load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@rules_go//go:def.bzl", "go_binary")

_PLATFORMS = {
    "linux_amd64": {"goos": "linux", "goarch": "amd64"},
    "linux_arm64": {"goos": "linux", "goarch": "arm64"},
    "darwin_arm64": {"goos": "darwin", "goarch": "arm64"},
}

def go_tools_tar(name, tools, package_dir = "/usr/bin", visibility = None):
    """Build Go binaries for each tools-image platform and put them in a tar.

    Args:
        name: Base name for generated targets.
        tools: Dict from installed command name to a go_library label.
        package_dir: Image directory that receives the commands.
        visibility: Visibility of generated platform targets.
    """
    for platform, constraints in _PLATFORMS.items():
        binaries = {}
        for command, library in tools.items():
            binary_name = "{}_{}_{}".format(name, command.replace("-", "_"), platform)
            go_binary(
                name = binary_name,
                embed = [library],
                goarch = constraints["goarch"],
                goos = constraints["goos"],
                pure = "on",
                tags = ["manual"],
            )
            binaries[command] = ":" + binary_name

        tar(
            name = name + "_" + platform,
            srcs = binaries.values(),
            tags = ["manual"],
            mtree = [
                "./{package_dir}/{command} type=file mode=0755 content=$(execpath {target})".format(
                    package_dir = package_dir.lstrip("/"),
                    command = command,
                    target = target,
                )
                for command, target in binaries.items()
            ],
            visibility = visibility,
        )
