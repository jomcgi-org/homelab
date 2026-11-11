"Force a dependency to build in the exec (host) configuration."

def _exec_filegroup_impl(ctx):
    """Passes through files from a dependency built in exec configuration.

    Use this to wrap platform-independent build outputs (e.g. JS/CSS/HTML from
    vite) so they aren't needlessly rebuilt under target platform transitions
    (e.g. py3_image's Linux platform transition).
    """
    return [DefaultInfo(files = ctx.attr.src[DefaultInfo].files)]

exec_filegroup = rule(
    implementation = _exec_filegroup_impl,
    attrs = {
        "src": attr.label(cfg = "exec", mandatory = True),
    },
)
