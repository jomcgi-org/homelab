"""stub_engine: an empty engine repository for platforms the host cannot use.

The semgrep and semgrep_pro module extensions declare one engine repository per
platform, and MODULE.bazel imports every one of them with use_repo. On a Linux
host the macOS engines are never selected, but declaring them as oci_archive
still makes any query that spans //... fetch their OCI manifests, and an
intermittent fetch failure then poisons the rdeps query CI uses to pick
affected targets (#5121). Declaring the foreign-platform engines as this stub
instead keeps the repository names resolvable without touching the network.
"""

def _stub_engine_impl(rctx):
    rctx.file("BUILD.bazel", rctx.attr.build_file_content)

stub_engine = repository_rule(
    implementation = _stub_engine_impl,
    attrs = {
        "build_file_content": attr.string(
            mandatory = True,
            doc = "BUILD content declaring the same targets the real engine repository exposes, over no files.",
        ),
    },
    doc = "An engine repository with no files, for platforms the current host cannot run.",
)

def host_is_macos(module_ctx):
    """True when the module extension runs on a macOS host."""
    return module_ctx.os.name.lower().startswith("mac")
