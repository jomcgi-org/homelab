load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

"""
Build supporting tools.

The tools subpackage is used to define build rules and configurations for tools
used throughout the repository. Firstparty tools not required for build support
should live elsewhere.
"""

load("@bazelrc-preset.bzl", "bazelrc_preset")

package(default_visibility = ["//visibility:public"])

bazelrc_preset(
    name = "preset",
    doc_link_template = "https://registry.build/flag/bazel?filter={flag}",
)

sh_binary(
    name = "workspace_status",
    srcs = ["workspace_status.sh"],
)

sh_binary(
    name = "hf_lock",
    srcs = ["hf_lock.sh"],
)

sh_binary(
    name = "help",
    srcs = ["help.sh"],
)
