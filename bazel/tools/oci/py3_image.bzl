"py3_image - single-platform Python OCI images"

load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@aspect_rules_py//py:defs.bzl", "py_image_layer")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load", "oci_push")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/oci:providers.bzl", "oci_image_info")

def py3_image(name, binary, main = None, root = "/", layer_groups = {}, env = {}, workdir = None, base = "@python_base", tars = [], bash_symlink = True, repository = None, visibility = ["//bazel/images:__pkg__"]):
    """Create a Python 3 image for the caller's CPU (amd64 in Linux CI).

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
        bash_symlink: Create /bin/bash -> /usr/bin/bash symlink layer. Set to False for bases
                     where /bin is a symlink to /usr/bin (e.g., Wolfi apko images), as the
                     layer would shadow the /bin symlink and break /bin/* resolution.
        repository: The container registry repository (e.g., "ghcr.io/jomcgi/homelab/my-app").
                   Defaults to "ghcr.io/jomcgi/homelab/{package_name}".
        visibility: Visibility of the generated .push target. Defaults to ["//bazel/images:__pkg__"]
                   to allow access from the auto-generated //images:push_all multirun.
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

    # A filegroup has no `.digest` output. Surface the image's digest under
    # the same transition the push target uses for Helm and the manifest.
    platform_transition_filegroup(
        name = name + ".digest",
        srcs = [name + "_image.digest"],
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
    sh_test(
        name = name + "_config_test",
        srcs = ["//bazel/tools/oci:verify-py3-image.sh"],
        args = ["$(rootpath {})".format(name + "_image")],
        data = [name + "_image"],
    )

    # Push the same platform-specific image used by .load.
    _repository = repository if repository else "ghcr.io/jomcgi/homelab/" + native.package_name()
    oci_push(
        name = name + ".push",
        image = name,
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
