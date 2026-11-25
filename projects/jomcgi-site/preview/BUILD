# The local site preview (README "Local preview", HOME-877). Agents run
# preview.sh from a checkout; Bazel only tests it. Hand-maintained.
# gazelle:ignore
# gazelle:exclude out
load("@rules_shell//shell:sh_test.bzl", "sh_test")

# preview.sh with fake pnpm, node and gh: arguments, exit codes, the one
# cleared output directory, signals, git and the one PR comment.
sh_test(
    name = "preview_test",
    srcs = ["preview_test.sh"],
    data = [
        "preview.sh",
        "//:.gitignore",
    ],
)
