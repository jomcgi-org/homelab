"""Module extension for lifting binaries out of digest-pinned OCI images.

Registers the `oci_binaries` repository rule for upstreams that publish a
program only as a container image, so there is no release tarball to
`multiarch_http_archive` and no Wolfi package to name in an apko.yaml.

Mirrors //bazel/tools/postgres:extensions.bzl, which does the same job for the
PostgreSQL test fixture.
"""

load(":oci_binaries.bzl", "oci_binaries")

def _oci_binaries_ext_impl(_module_ctx):
    # Apache Iggy's server binary, for the EmberVM stateful iggy runtime image
    # (ADR embervm/039). Docker Hub is the server's ONLY durable distribution
    # channel: upstream's .github/config/publish.yml routes the `server-*` tag
    # to `registry: dockerhub`, and the GitHub-built `iggy-<target>-<ver>.tar.gz`
    # bundles are `actions/upload-artifact` CI artifacts with a 30-day retention,
    # not release assets with a stable URL. The image is pinned by digest in
    # MODULE.bazel's oci.pull, so this extraction is reproducible.
    #
    # The upstream image is debian:trixie-slim with libhwloc/libudev installed,
    # but the binary it carries is built for x86_64-unknown-linux-musl and is
    # fully static (no PT_INTERP, no DT_NEEDED), so none of that base comes with
    # it and the runtime image stays a thin Wolfi rootfs. oci_binaries asserts
    # the static property on every fetch rather than trusting it.
    oci_binaries(
        name = "iggy_server_bin",
        image = "@iggy_server_image_linux_amd64//:index.json",
        binaries = {
            # The server. PID 1 of the guest (ember-iggy-init) execs this.
            "usr/local/bin/iggy-server": "usr/local/bin/iggy-server",
            # The CLI, for in-guest debugging over the local TCP port. Small
            # (13 MiB) and the same static build, so it costs one layer entry.
            "usr/local/bin/iggy": "usr/local/bin/iggy",
        },
    )

oci_binaries_ext = module_extension(implementation = _oci_binaries_ext_impl)
