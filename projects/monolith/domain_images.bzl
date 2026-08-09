"""Build-only per-domain OCI images for the FastMonolith composition.

Every domain in MONOLITH_DOMAINS gets a dual-arch image target
(``//projects/monolith:image_domain_<name>``) that runs ``app/main_domain.py``
with ``MONOLITH_DOMAIN=<name>`` baked in, composing just that domain via
``build_app(domain_profile(name), [MODULE])``.

Deliberate differences from the ``py3_image`` macro these mirror:

- **One shared layer set.** All domain images reference the SAME
  ``py_image_layer`` targets (they share one binary), so adding a domain costs
  an image-config action, not another full-venv tar. The layers dedupe across
  the 18 domain images only, NOT against ``:image``: py_image_layer embeds the
  binary name (``main`` vs ``main_domain``) in every layer path, so pushing
  these would upload a fresh layer set.
- **Tagged ``manual``.** ``bazel test //...`` builds every non-manual target
  it matches, so without the tag every PR would pay for 18 domains x 2 arches
  of image config + layer assembly that nothing in CI consumes.
- **No ``oci_push`` target.** These are build/deploy-on-demand artifacts (ADR
  services/010 keeps per-domain production charts out of scope). Skipping the
  push target keeps them out of the auto-generated ``//images:push_all``
  multirun and its CI validator, which requires every ``oci_push`` in the
  graph to be push_all-listed.
- **One canary config sh_test.** The env formulas here are a hand-copied
  sibling of py3_image's, and every image target is manual, so without a
  canary nothing in CI would notice the formulas rotting (e.g. a venv layout
  change). One domain's amd64 image runs the same verify-py3-image.sh check
  the pushed images use; the other 17 differ only in the MONOLITH_DOMAIN env
  value.

Keep MONOLITH_DOMAINS in sync with ``DOMAIN_NAMES`` in
``app/modules_private.py`` (app/main_domain_test.py asserts the parity).
"""

load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@aspect_rules_py//py:defs.bzl", "py_image_layer")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_image_index")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

MONOLITH_DOMAINS = [
    "home",
    "chat",
    "knowledge",
    "scheduler",
    "ships",
    "grimoire",
    "hikes",
    "stars",
    "trips",
    "dr_jobs",
    "campsites",
    "agent_sessions",
    "worldcup",
    "artifact",
    "faas",
    "graph",
    "demos",
    "ember_public",
    "agent",
    "cluster",
    "semgrep_scan",
    "sandbox",
]

def monolith_domain_images(
        name,
        binary,
        main,
        domains,
        base = "@python_base",
        config_test_domain = None,
        visibility = ["//:__subpackages__"]):
    """One dual-arch, build-only OCI image per domain from a shared binary.

    Args:
        name: Prefix for the shared targets; also the name of a filegroup
            aggregating every ``image_domain_<d>`` index (one-command build).
        binary: The shared ``py_venv_binary`` (app/main_domain.py entrypoint).
        main: The entrypoint source file, layered in explicitly because
            ``py_venv_binary`` omits ``ctx.file.main`` from runfiles.
        domains: Domain package names; each must export ``MODULE``.
        base: Base image.
        config_test_domain: Domain whose amd64 image gets a non-manual
            verify-py3-image.sh config test (the CI canary for the env
            formulas). None skips the canary.
        visibility: Visibility of the per-domain image targets.
    """
    binary_label = native.package_relative_label(binary)
    binary_path = "/{}/{}".format(binary_label.package, binary_label.name)
    runfiles_dir = binary_path + ".runfiles"
    workspace_root = runfiles_dir + "/_main"
    package_root = workspace_root + "/" + binary_label.package

    shared_env = {
        "BAZEL_WORKSPACE": "_main",
        "RUNFILES_DIR": runfiles_dir,
        # The workspace root plus the project directory, matching the
        # imports = ["."] used by py_venv_binary (mirrors :image / :image_public).
        "PYTHONPATH": workspace_root + ":" + package_root,
        # The minimal base image has no CA certificates; point OpenSSL at
        # certifi's bundle so outbound HTTPS works.
        "SSL_CERT_FILE": package_root + "/." + binary_label.name +
                         "/lib/python3.13/site-packages/certifi/cacert.pem",
    }

    # Wolfi installs bash to /usr/bin/bash but py_venv_binary shebangs use
    # /bin/bash (same layer py3_image creates).
    tar(
        name = name + "_bash_symlink",
        mtree = ["./bin/bash type=link link=/usr/bin/bash"],
    )

    # py_venv_binary omits ctx.file.main from runfiles; layer the entrypoint
    # source at its runfiles path (same supplementary layer py3_image creates).
    main_label = "//{}:{}".format(binary_label.package, main)
    tar(
        name = name + "_srcs",
        srcs = [main_label],
        mtree = [
            ".{}/{} type=file content=$(execpath {})".format(
                package_root,
                main,
                main_label,
            ),
        ],
    )

    # ONE shared layer set for every domain image (the binary is shared, so
    # the layers are identical; per-arch content comes from the platform
    # transition on each consuming image).
    layers = py_image_layer(
        name = name + "_layers",
        binary = binary,
        root = "/",
    )
    extra = [name + "_bash_symlink", name + "_srcs"]

    images = []
    for d in domains:
        env = dict(shared_env)
        env["MONOLITH_DOMAIN"] = d

        oci_image(
            name = "{}_{}_base_amd64".format(name, d),
            base = base,
            tags = ["manual"],
            tars = layers + extra,
            entrypoint = [binary_path],
            env = env,
            workdir = workspace_root,
        )
        platform_transition_filegroup(
            name = "{}_{}_amd64".format(name, d),
            srcs = ["{}_{}_base_amd64".format(name, d)],
            tags = ["manual"],
            target_platform = "//bazel/tools/platforms:linux_x86_64",
        )

        oci_image(
            name = "{}_{}_base_arm64".format(name, d),
            base = base,
            tags = ["manual"],
            tars = layers + extra,
            entrypoint = [binary_path],
            env = env,
            workdir = workspace_root,
        )
        platform_transition_filegroup(
            name = "{}_{}_arm64".format(name, d),
            srcs = ["{}_{}_base_arm64".format(name, d)],
            tags = ["manual"],
            target_platform = "//bazel/tools/platforms:linux_aarch64",
        )

        oci_image_index(
            name = "image_domain_" + d,
            images = [
                "{}_{}_amd64".format(name, d),
                "{}_{}_arm64".format(name, d),
            ],
            tags = ["manual"],
            visibility = visibility,
        )
        images.append("image_domain_" + d)

        if d == config_test_domain:
            # CI canary: the only non-manual consumer of the shared layers,
            # verifying entrypoint/PYTHONPATH/RUNFILES_DIR exactly as the
            # pushed images' config tests do.
            sh_test(
                name = "image_domain_{}_config_test".format(d),
                srcs = ["//bazel/tools/oci:verify-py3-image.sh"],
                args = ["$(rootpath {}_{}_base_amd64)".format(name, d)],
                data = ["{}_{}_base_amd64".format(name, d)],
            )

    # bazel build //projects/monolith:domain_images builds every domain image.
    # Tagged manual (like each image target) so `bazel test //...` on every PR
    # does not build 18 domains x 2 arches of image nobody consumes in CI; the
    # composition itself is still CI-covered by app/main_domain_test.py.
    native.filegroup(
        name = name,
        srcs = images,
        tags = ["manual"],
        visibility = visibility,
    )
