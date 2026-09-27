load("@rules_uv//uv:pip.bzl", "pip_compile")

exports_files(
    ["all.txt"],
    visibility = ["//visibility:public"],
)

pip_compile(
    name = "runtime",
    requirements_in = "//:pyproject.toml",
    requirements_overrides = "overrides.txt",
    requirements_txt = "runtime.txt",
    tags = ["no-remote"],  # Requires internet access, skip on remote executors
    universal = True,  # Generate cross-platform lock file (macOS + Linux)
)

pip_compile(
    name = "requirements.all",
    data = glob([
        "*.in",
        "*.txt",
    ]),
    extra_args = [
        # semgrep's sdist requires semgrep-core at build time, which is only
        # available inside the wheel. Restrict to pre-built wheels only.
        "--only-binary",
        "semgrep",
    ],
    requirements_in = "all.in",
    requirements_overrides = "overrides.txt",
    requirements_txt = "all.txt",
    tags = ["no-remote"],  # Requires internet access, skip on remote executors
    universal = True,  # Generate cross-platform lock file (macOS + Linux)
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
