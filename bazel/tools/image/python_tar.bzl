"""python_tar - Package Python runtime + pip deps into per-platform tars.

Uses py_image_layer from @aspect_rules_py to package a shim py_venv_binary's
entire dependency tree (interpreter, stdlib, pip packages) into tar layers.
Platform transitions ensure the correct platform-specific Python toolchain
and pip wheels are selected for each target.

The tars use Bazel's runfiles layout — the Python interpreter and packages
live under the binary's .runfiles/ tree. The tools image BUILD (Task 5)
adds a wrapper script at /usr/bin/python3 that execs the real interpreter.

Produces for each platform:
  :{name}_linux_amd64 - platform-transitioned py_image_layer tars
  :{name}_linux_arm64 - platform-transitioned py_image_layer tars
  :{name}_darwin_arm64 - platform-transitioned py_image_layer tars
"""

load("@aspect_rules_py//py:defs.bzl", "py_image_layer")

# Map from our platform keys to //bazel/tools/platforms targets.
_PLATFORMS = {
    "linux_amd64": "//bazel/tools/platforms:linux_x86_64",
    "linux_arm64": "//bazel/tools/platforms:linux_aarch64",
    "darwin_arm64": "//bazel/tools/platforms:darwin_aarch64",
}

def python_tar(name, binary, root = "/", visibility = None):
    """Package Python runtime + pip deps into per-platform tars.

    For each platform, creates py_image_layer tars containing the Python
    interpreter, stdlib, and all pip dependencies declared by the binary.

    The py_image_layer macro handles platform transitions internally via its
    `platform` parameter, selecting the correct Python toolchain and pip
    wheels for each target platform.

    Args:
        name: Base name for the generated targets.
        binary: A py_venv_binary (or py_binary) target whose deps include
                all desired pip packages. The binary itself is just a vehicle
                for declaring the dependency tree.
        root: Root directory for the tar layout. Default: "/"
        visibility: Visibility of the generated targets.

    Creates (for each platform linux_amd64, linux_arm64, darwin_arm64):
        :{name}_{platform} - platform-transitioned filegroup of the
            py_image_layer tars (interpreter + packages + default layers)
    """
    for platform_key, platform_target in _PLATFORMS.items():
        # py_image_layer creates tar layers (interpreter, packages, default)
        # and wraps them in a platform_transition_filegroup when the platform
        # parameter is set. The name target is the platform-transitioned
        # filegroup containing all layer tars.
        py_image_layer(
            name = name + "_" + platform_key,
            binary = binary,
            root = root,
            platform = platform_target,
            visibility = visibility,
        )
