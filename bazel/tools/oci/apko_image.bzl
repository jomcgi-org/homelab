"apko_image - multi-platform apko OCI images"

load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_apko//apko:defs.bzl", _apko_image = "apko_image")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_image_index", "oci_push")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/oci:apko_push.bzl", "apko_push")
load("//bazel/tools/oci:oci_run.bzl", "oci_run")
load("//bazel/tools/oci:providers.bzl", "oci_image_info")

def apko_image(
        name,
        config,
        contents,
        repository = None,
        visibility = ["//bazel/images:__pkg__"],
        tars = None,
        multiarch_tars = None,
        multiplatform_tars = None,
        arm64 = True):
    """Create a multi-platform apko OCI image, optionally with additional tar layers.

    Args:
        name: The name of the image.
        config: The apko config file (should define both x86_64 and aarch64 in archs
                list, unless arm64 = False, in which case only x86_64).
        arm64: Whether to build the aarch64 variant. Defaults to True (dual-arch).
               Set False for images that carry an architecture-specific artifact the
               build cannot produce for arm64 (e.g. a compiled NIF built on the amd64
               executor); the apko config's archs list must then be x86_64-only too.
               When False, :{name} is a single-platform amd64 image, not an index.
        contents: The apko contents (lock file).
        repository: The container registry repository (e.g., "ghcr.io/jomcgi/homelab/my-app").
                   Defaults to "ghcr.io/jomcgi/homelab/{package_name}".
        visibility: Visibility of the generated .push target. Defaults to ["//bazel/images:__pkg__"]
                   to allow access from the auto-generated //images:push_all multirun.
        tars: Optional list of regular tar targets (used for all platforms).
              Example: [":node_modules_tar", ":config_tar"]
        multiarch_tars: Optional list of multiarch tar base names.
                       For each base name, apko_image will use {base}_amd64 and {base}_arm64.
                       Example: [":ttyd_tar"] will use :ttyd_tar_amd64 and :ttyd_tar_arm64
        multiplatform_tars: DEPRECATED. Use tars and multiarch_tars instead.
                           Optional list of multiplatform tar dicts from multiplatform_tar().
                           Each dict should have "amd64" and/or "arm64" keys with tar targets.

    Creates:
        :{name} - The apko image target (or oci_image_index if tars are provided)
        :{name}.push - Target to push image to registry
        :{name}.run - Target to run image locally (without pushing)
        :{name}_lock_test - Test that verifies lock file is in sync with config

    Examples:
        # Using new API
        multiarch_binary_tar(
            name = "ttyd_tar",
            amd64 = ":ttyd_amd64_file",
            arm64 = ":ttyd_arm64_file",
            binary_name = "ttyd",
        )

        pkg_tar(
            name = "node_modules_tar",
            srcs = ["//:claude_code"],
            package_dir = "/usr/local/lib",
        )

        apko_image(
            name = "my_image",
            config = "apko.yaml",
            contents = "@apko_lock//:contents",
            tars = [":node_modules_tar"],
            multiarch_tars = [":ttyd_tar"],
        )

    Notes:
        When multiplatform_tars are provided, separate platform-specific apko images are created
        and layered, then combined into a multi-platform index. This is necessary because apko's
        native multi-platform support doesn't allow adding platform-specific files after the fact.
    """

    # Build platform-specific tar lists
    tars_amd64 = []
    tars_arm64 = []

    # Handle regular tars (used for both platforms)
    if tars:
        tars_amd64.extend(tars)
        tars_arm64.extend(tars)

    # Handle multiarch tars (use _amd64/_arm64 suffixes)
    if multiarch_tars:
        for tar_base in multiarch_tars:
            tars_amd64.append(tar_base + "_amd64")
            tars_arm64.append(tar_base + "_arm64")

    # Handle deprecated multiplatform_tars format
    if multiplatform_tars:
        for tar_dict in multiplatform_tars:
            if "amd64" in tar_dict:
                tars_amd64.append(tar_dict["amd64"])
            if "arm64" in tar_dict:
                tars_arm64.append(tar_dict["arm64"])

    # Build EVERY apko image via rules_oci (oci_image per arch + oci_image_index
    # for multi-arch), even when no extra tars are layered. This gives every image
    # a rules_oci `:{name}.digest` target (the content-addressed index digest),
    # which the chart pins so the deployed reference is digest-stable and a chart
    # republish rolls a container only when its content actually changes. A raw
    # rules_apko multi-arch image emits a FLAT per-arch manifest list whose index
    # digest is not directly readable by jq; wrapping in oci_image_index yields a
    # single-entry index whose digest is. Empty tar lists are valid, so the
    # no-tars case is just the general path with tars = [].

    # Create the amd64 base image (always built).
    _apko_image(
        name = name + "_base_amd64",
        architecture = "x86_64",
        config = config,
        contents = contents,
        tag = "latest",
    )

    # Transition the base image to its target platform
    # (apko bases are already platform-specific, this just sets the platform metadata)
    platform_transition_filegroup(
        name = name + "_base_amd64_transitioned",
        srcs = [":" + name + "_base_amd64"],
        target_platform = "@rules_go//go/toolchain:linux_amd64",
    )

    if arm64:
        _apko_image(
            name = name + "_base_arm64",
            architecture = "aarch64",
            config = config,
            contents = contents,
            tag = "latest",
        )

        platform_transition_filegroup(
            name = name + "_base_arm64_transitioned",
            srcs = [":" + name + "_base_arm64"],
            target_platform = "@rules_go//go/toolchain:linux_arm64",
        )

        # Layer tars on top of the transitioned bases (empty list when no tars
        # given). Note: tars are NOT transitioned because they contain
        # platform-independent files (e.g. JavaScript bundles, config files)
        # that are built on the exec platform.
        oci_image(
            name = name + "_amd64",
            base = ":" + name + "_base_amd64_transitioned",
            tars = tars_amd64,
        )

        oci_image(
            name = name + "_arm64",
            base = ":" + name + "_base_arm64_transitioned",
            tars = tars_arm64,
        )

        # Create multi-platform index
        oci_image_index(
            name = name,
            images = [
                ":" + name + "_amd64",
                ":" + name + "_arm64",
            ],
        )
    else:
        # Single-arch (amd64-only): :{name} is the amd64 image directly, no
        # index. Used when a layered tar carries an amd64-specific artifact
        # (e.g. a compiled NIF) that has no valid aarch64 counterpart.
        oci_image(
            name = name,
            base = ":" + name + "_base_amd64_transitioned",
            tars = tars_amd64,
        )

    push_image = name
    use_oci_push = True

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

    # Push target - uses oci_push when layering tars, apko_push for native apko images
    _repository = repository if repository else "ghcr.io/jomcgi/homelab/" + native.package_name()
    if use_oci_push:
        oci_push(
            name = name + ".push",
            image = ":" + push_image,
            repository = _repository,
            remote_tags = name + "_stamped_tags",
            visibility = visibility,
        )
    else:
        apko_push(
            name = name + ".push",
            image = push_image,
            repository = _repository,
            remote_tags = name + "_stamped_tags",
            visibility = visibility,
        )

    # Expose OciImageInfo provider for use by helm_chart(images = {...}). The
    # image is referenced so the chart-version bot's dependency closure reaches
    # content layered into the image (e.g. fc-agent-init bundled into the harness
    # image), not just this package; see oci_image_info in providers.bzl.
    oci_image_info(
        name = name + ".info",
        repository = _repository,
        image_tags = name + "_stamped.tags.txt",
        image_digest = ":" + name + ".digest",
        image = ":" + name,
        visibility = ["//visibility:public"],
    )

    # Create .run target for local testing
    oci_run(
        name = name + ".run",
        image = ":" + name,
    )

    # Verify lock file checksum matches config
    # Catches stale lock files in CI via `bazel test //...`
    lock = config.replace(".yaml", ".lock.json")
    sh_test(
        name = name + "_lock_test",
        srcs = ["//bazel/tools/oci:verify-apko-lock.sh"],
        args = [
            "$(location " + config + ")",
            "$(location " + lock + ")",
        ],
        data = [config, lock],
    )
