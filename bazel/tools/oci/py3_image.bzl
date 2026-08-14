"py_image - multi-platform Python OCI images"

load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@aspect_rules_py//py:defs.bzl", "py_image_layer")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_image_index", "oci_load", "oci_push")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/oci:providers.bzl", "oci_image_info")

def py3_image(name, binary, main = None, root = "/", layer_groups = {}, env = {}, workdir = None, base = "@python_base", tars = [], multiarch_tars = [], bash_symlink = True, repository = None, visibility = ["//bazel/images:__pkg__"], multi_platform = True):
    """Create a multi-platform Python 3 image from a Python binary.

    Args:
        name: The name of the image.
        binary: The Python binary to create the image from.
        main: The main .py source file for the binary. Auto-derived as "{binary_name}.py"
              for same-package binaries. Set explicitly for non-standard naming. Cross-package
              binaries are skipped (their sources are in transitive deps).
        root: The root directory where everything will be put into
        layer_groups: The layer groups to use for the image.
        env: The environment variables to set in the image.
        workdir: The working directory to set in the image.
        base: The base image to use for the image.
        tars: Additional tar layers to include in the image (e.g., extra binaries or config).
        multiarch_tars: Optional list of multiarch tar base names. For each base name, py3_image
                       will use {base}_amd64 and {base}_arm64 targets.
                       Example: ["@some_cli//:tar"] uses @some_cli//:tar_amd64 and @some_cli//:tar_arm64
        bash_symlink: Create /bin/bash -> /usr/bin/bash symlink layer. Set to False for bases
                     where /bin is a symlink to /usr/bin (e.g., Wolfi apko images), as the
                     layer would shadow the /bin symlink and break /bin/* resolution.
        repository: The container registry repository (e.g., "ghcr.io/jomcgi/homelab/my-app").
                   Defaults to "ghcr.io/jomcgi/homelab/{package_name}".
        visibility: Visibility of the generated .push target. Defaults to ["//bazel/images:__pkg__"]
                   to allow access from the auto-generated //images:push_all multirun.
        multi_platform: Build for both amd64 and arm64. Defaults to True.
    """
    binary = native.package_relative_label(binary)
    binary_path = "{}{}/{}".format(root, binary.package, binary.name)
    runfiles_dir = "{}.runfiles".format(binary_path)
    repo_name = binary.repo_name or "_main"
    workspace_root = "{}/{}".format(runfiles_dir, repo_name)
    env = dict({
        "BAZEL_WORKSPACE": repo_name,
        "RUNFILES_DIR": runfiles_dir,
        "PYTHONPATH": workspace_root,
    }, **env)

    # py_venv_binary omits ctx.file.main from runfiles — create a supplementary
    # tar layer to include the source file at the correct runfiles path.
    extra_tars = list(tars)
    if bash_symlink:
        # Wolfi installs bash to /usr/bin/bash but py_venv_binary shebangs use /bin/bash.
        # Skip for bases where /bin -> /usr/bin (the layer would shadow the symlink).
        tar(
            name = name + "_bash_symlink",
            mtree = ["./bin/bash type=link link=/usr/bin/bash"],
        )
        extra_tars.append(name + "_bash_symlink")
    if main == None and binary.package == native.package_name():
        main = binary.name + ".py"
    if main:
        main_label = str(binary).rsplit(":", 1)[0] + ":" + main
        source_dest = ".{}/{}/{}".format(workspace_root, binary.package, main)
        tar(
            name = name + "_srcs",
            srcs = [main_label],
            mtree = [
                "{} type=file content=$(execpath {})".format(source_dest, main_label),
            ],
        )
        extra_tars.append(name + "_srcs")

    extra_tars_amd64 = list(extra_tars)
    extra_tars_arm64 = list(extra_tars)
    for tar_base in multiarch_tars:
        extra_tars_amd64.append(tar_base + "_amd64")
        extra_tars_arm64.append(tar_base + "_arm64")

    if multi_platform:
        # Build AMD64 image.
        #
        # tags = ["manual"]: this target and the transitioned filegroup below
        # are the same work in two configurations, and `bazel test //...`
        # expands to both, so each base was assembled twice. `manual` removes
        # it from wildcard expansion only; the filegroup still depends on it.
        # Same defect as apko_image.bzl, though cheaper here because oci_image
        # is remotely executable where the apko action is `no-remote-exec`.
        oci_image(
            name = name + "_base_amd64",
            tags = ["manual"],
            base = base,
            tars = py_image_layer(
                name = name + "_layers_amd64",
                binary = binary,
                root = root,
                layer_groups = layer_groups,
            ) + extra_tars_amd64,
            entrypoint = [binary_path],
            env = env,
            workdir = workdir or workspace_root,
        )
        platform_transition_filegroup(
            name = name + "_amd64",
            srcs = [name + "_base_amd64"],
            target_platform = "//bazel/tools/platforms:linux_x86_64",
        )

        # Build ARM64 image. See the amd64 base above for why this is manual.
        oci_image(
            name = name + "_base_arm64",
            tags = ["manual"],
            base = base,
            tars = py_image_layer(
                name = name + "_layers_arm64",
                binary = binary,
                root = root,
                layer_groups = layer_groups,
            ) + extra_tars_arm64,
            entrypoint = [binary_path],
            env = env,
            workdir = workdir or workspace_root,
        )
        platform_transition_filegroup(
            name = name + "_arm64",
            srcs = [name + "_base_arm64"],
            target_platform = "//bazel/tools/platforms:linux_aarch64",
        )

        # Create multi-platform index
        oci_image_index(
            name = name,
            images = [
                name + "_amd64",
                name + "_arm64",
            ],
        )

        # Load uses host platform
        platform_transition_filegroup(
            name = name + "_platform",
            srcs = select({
                "@platforms//cpu:arm64": [name + "_base_arm64"],
                "@platforms//cpu:x86_64": [name + "_base_amd64"],
            }),
            target_platform = select({
                "@platforms//cpu:arm64": "//bazel/tools/platforms:linux_aarch64",
                "@platforms//cpu:x86_64": "//bazel/tools/platforms:linux_x86_64",
            }),
        )
        oci_load(
            name = name + ".load",
            image = name + "_platform",
            repo_tags = [native.package_name() + ":latest"],
        )
    else:
        # Single platform build (legacy)
        oci_image(
            name = name + "_image",
            base = base,
            tars = py_image_layer(
                name = name + "_layers",
                binary = binary,
                root = root,
                layer_groups = layer_groups,
            ) + extra_tars,
            entrypoint = [binary_path],
            env = env,
            workdir = workdir or workspace_root,
        )
        platform_transition_filegroup(
            name = name,
            srcs = [name + "_image"],
            target_platform = select({
                "@platforms//cpu:arm64": "//bazel/tools/platforms:linux_aarch64",
                "@platforms//cpu:x86_64": "//bazel/tools/platforms:linux_x86_64",
            }),
        )
        oci_load(
            name = name + ".load",
            image = name,
            repo_tags = [native.package_name() + ":latest"],
        )

    # One tag per push: the timestamped build tag, which is also what helm
    # values read via `head -1`. A second branch-name tag used to be applied
    # under --define=CI=true; its only documented consumer was ArgoCD Image
    # Updater filtering, and this cluster has no Image Updater. Charts deploy by
    # repository@digest (bazel/helm/images.bzl), so a tag is never the deployed
    # reference.
    expand_template(
        name = name + "_stamped_tags",
        out = name + "_stamped.tags.txt",
        template = [
            "{STABLE_IMAGE_TAG}",  # Timestamp: YYYY.MM.DD.HH.MM.SS-shortsha
        ],
        stamp_substitutions = {
            "{STABLE_IMAGE_TAG}": "{{STABLE_IMAGE_TAG}}",
        },
    )

    # Verify image config has correct Python runtime environment.
    # Tests one platform (amd64) since the config is identical across architectures.
    sh_test(
        name = name + "_config_test",
        srcs = ["//bazel/tools/oci:verify-py3-image.sh"],
        args = ["$(rootpath {})".format(
            name + "_base_amd64" if multi_platform else name + "_image",
        )],
        data = [name + "_base_amd64" if multi_platform else name + "_image"],
    )

    # Push uses the index for multi-platform, or platform-specific for single platform
    _repository = repository if repository else "ghcr.io/jomcgi/homelab/" + native.package_name()
    oci_push(
        name = name + ".push",
        image = name if multi_platform else name,
        repository = _repository,
        remote_tags = name + "_stamped_tags",
        visibility = visibility,
    )

    # Expose OciImageInfo provider for use by helm_chart(images = {...}).
    # image is referenced so the chart-version bot's dependency closure reaches
    # the application sources layered into the image (parity with apko_image);
    # without it a py-only code change is invisible to both the PR auto-bump
    # and the main-branch missed-bump guard, and a merge deploys nothing
    # (chart 0.285.5 / d263cefa3 incident, 2026-07-04).
    oci_image_info(
        name = name + ".info",
        repository = _repository,
        image_tags = name + "_stamped.tags.txt",
        image_digest = ":" + name + ".digest",
        image = ":" + name,
        visibility = ["//visibility:public"],
    )
