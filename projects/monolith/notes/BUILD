# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(
        ["**/*.py"],
        allow_empty = True,
    ),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

filegroup(
    name = "production_sources",
    srcs = glob(
        ["**/*.py"],
        allow_empty = True,
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "non_test_python_sources",
    srcs = glob(
        ["**/*.py"],
        allow_empty = True,
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "**/conftest.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
