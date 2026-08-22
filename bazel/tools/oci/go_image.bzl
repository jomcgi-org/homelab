"go_image macro for multi-platform OCI containers"

load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_image_index", "oci_load", "oci_push")
load("//bazel/tools/oci:providers.bzl", "oci_image_info")

def go_image(name, binary, base = "@distroless_base", repository = None, extra_tars = [], visibility = ["//bazel/images:__pkg__"], multi_platform = False):
    """Create a multi-platform Go OCI image from a Go binary.

    Args:
        name: The name of the image.
        binary: The Go binary target to package.
        base: The base image to use. Defaults to distroless base.
        repository: The container registry repository (e.g., "ghcr.io/jomcgi/homelab/my-app").
                   Defaults to "ghcr.io/jomcgi/homelab/{package_name}".
        extra_tars: Platform-independent tar layers to include in the image (e.g., static
                   assets). These are added AFTER the platform transition so they are not
                   cross-compiled. Defaults to [].
        visibility: Visibility of the generated .push target. Defaults to ["//bazel/images:__pkg__"]
                   to allow access from the auto-generated //images:push_all multirun.
        multi_platform: Build for both amd64 and arm64. Defaults to False: every
            node is amd64 and no chart pins an arch, so the arm64 half had no
            consumer and doubled every layer tar and image action on each change.

    Creates:
        :{name} - The oci_image target (or oci_image_index for multi-platform)
        :{name}_amd64 - AMD64-specific image (if multi_platform=True)
        :{name}_arm64 - ARM64-specific image (if multi_platform=True)
        :{name}.load - Target to load image into local Docker
        :{name}.push - Target to push image to registry
    """
    if multi_platform:
        for arch in ["amd64", "arm64"]:
            # Package binary into a tar layer
            tar(
                name = name + "_app_layer_" + arch,
                srcs = [binary],
                mtree = [
                    "./opt/app type=file content=$(execpath {})".format(binary),
                ],
            )

            # Create image with binary layer
            oci_image(
                name = name + "_bin_" + arch,
                base = base,
                tars = [name + "_app_layer_" + arch],
                entrypoint = ["/opt/app"],
                user = "65532",  # nonroot user in distroless
            )

            # Cross-compile: transition to target platform
            platform_transition_filegroup(
                name = name + "_bin_transitioned_" + arch,
                srcs = [name + "_bin_" + arch],
                target_platform = "@rules_go//go/toolchain:linux_" + arch,
            )

            if extra_tars:
                # Layer extra tars AFTER the platform transition so they are
                # built on the host platform (not cross-compiled).
                oci_image(
                    name = name + "_base_" + arch,
                    base = name + "_bin_transitioned_" + arch,
                    tars = extra_tars,
                )
            else:
                native.alias(
                    name = name + "_base_" + arch,
                    actual = name + "_bin_transitioned_" + arch,
                )

        # Create multi-platform index
        oci_image_index(
            name = name,
            images = [
                name + "_base_amd64",
                name + "_base_arm64",
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
                "@platforms//cpu:arm64": "@rules_go//go/toolchain:linux_arm64",
                "@platforms//cpu:x86_64": "@rules_go//go/toolchain:linux_amd64",
            }),
        )
        oci_load(
            name = name + ".load",
            image = name + "_platform",
            repo_tags = [native.package_name() + ":latest"],
        )
    else:
        # Single platform build (legacy)
        tar(
            name = name + "_app_layer",
            srcs = [binary],
            mtree = [
                "./opt/app type=file content=$(execpath {})".format(binary),
            ],
        )
        oci_image(
            name = name + "_bin",
            base = base,
            tars = [name + "_app_layer"],
            entrypoint = ["/opt/app"],
            user = "65532",  # nonroot user in distroless
        )
        platform_transition_filegroup(
            name = name + "_bin_platform",
            srcs = [name + "_bin"],
            target_platform = select({
                "@platforms//cpu:arm64": "@rules_go//go/toolchain:linux_arm64",
                "@platforms//cpu:x86_64": "@rules_go//go/toolchain:linux_amd64",
            }),
        )
        if extra_tars:
            # An oci_image, so rules_oci declares `:{name}.digest` itself.
            oci_image(
                name = name,
                base = name + "_bin_platform",
                tars = extra_tars,
            )
        else:
            native.alias(
                name = name,
                actual = name + "_bin_platform",
            )

            # See py3_image.bzl: an alias has no `.digest` output, and the
            # digest helm pins must come from the same transition the push
            # target consumes.
            platform_transition_filegroup(
                name = name + ".digest",
                srcs = [name + "_bin.digest"],
                target_platform = select({
                    "@platforms//cpu:arm64": "@rules_go//go/toolchain:linux_arm64",
                    "@platforms//cpu:x86_64": "@rules_go//go/toolchain:linux_amd64",
                }),
            )
        native.alias(
            name = name + "_platform",
            actual = name,
        )
        oci_load(
            name = name + ".load",
            image = name + "_platform",
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

    # Push uses the index for multi-platform, or platform-specific for single platform
    _repository = repository if repository else "ghcr.io/jomcgi/homelab/" + native.package_name()
    oci_push(
        name = name + ".push",
        image = name if multi_platform else name + "_platform",
        repository = _repository,
        remote_tags = name + "_stamped_tags",
        visibility = visibility,
    )

    # Expose OciImageInfo provider for use by helm_chart(images = {...}).
    # image is referenced so the chart-version bot's dependency closure reaches
    # the application sources layered into the image (parity with apko_image);
    # without it a go-only code change is invisible to both the PR auto-bump
    # and the main-branch missed-bump guard.
    oci_image_info(
        name = name + ".info",
        repository = _repository,
        image_tags = name + "_stamped.tags.txt",
        image_digest = ":" + name + ".digest",
        image = ":" + name,
        visibility = ["//visibility:public"],
    )
