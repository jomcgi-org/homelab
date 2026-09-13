"""Target transition for the noded arm64 image binary."""

def _arm64_transition_impl(_settings, attr):
    return {
        "//command_line_option:platforms": str(attr.target_platform),
        # The amd64 noded build analyzes the same source graph with nogo. Avoid
        # rules_go supplying that amd64 analyzer binary to the native arm64
        # executor selected for this transitioned image build.
        "@rules_go//go/private:bootstrap_nogo": True,
    }

_arm64_transition = transition(
    implementation = _arm64_transition_impl,
    inputs = [],
    outputs = [
        "//command_line_option:platforms",
        "@rules_go//go/private:bootstrap_nogo",
    ],
)

def _arm64_platform_transition_filegroup_impl(ctx):
    files = [src[DefaultInfo].files for src in ctx.attr.srcs]
    runfiles = ctx.runfiles().merge_all([
        src[DefaultInfo].default_runfiles
        for src in ctx.attr.srcs
    ])
    return [DefaultInfo(
        files = depset(transitive = files),
        runfiles = runfiles,
    )]

arm64_platform_transition_filegroup = rule(
    implementation = _arm64_platform_transition_filegroup_impl,
    attrs = {
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
        "srcs": attr.label_list(
            allow_empty = False,
            allow_files = True,
            cfg = _arm64_transition,
        ),
        "target_platform": attr.label(mandatory = True),
    },
)
