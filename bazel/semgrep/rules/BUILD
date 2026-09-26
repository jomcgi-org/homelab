# gazelle:exclude golang
# gazelle:exclude python
# gazelle:exclude typescript
# gazelle:exclude yaml

package(default_visibility = ["//visibility:public"])

# The repo's own Semgrep rules. Nothing runs them as Bazel tests: CI scanning
# is Semgrep Managed Scans (registry rules only). Their one consumer is the
# semgrep guest image, which lays them under /etc/semgrep/rules/<subdir>/ for
# the semgrep-scan MCP tool, preserving the per-language subdirs because
# basenames collide across them.
filegroup(
    name = "local_rules",
    srcs = glob(
        [
            "**/*.yaml",
            "**/*.json",
        ],
        allow_empty = True,
    ),
)
