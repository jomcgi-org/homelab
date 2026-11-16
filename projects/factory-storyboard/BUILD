load("@aspect_rules_js//js:defs.bzl", "js_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Hand-maintained snapshot contract, fixtures and their conformance test.
# gazelle:ignore

filegroup(
    name = "schema",
    srcs = glob(["schema/**"]),
    visibility = ["//projects/factory-storyboard:__subpackages__"],
)

filegroup(
    name = "fixtures",
    srcs = glob(["fixtures/**/*.json"]),
    visibility = ["//projects/factory-storyboard:__subpackages__"],
)

py_test(
    name = "schema_test",
    srcs = ["schema_test.py"],
    data = [
        ":fixtures",
        ":schema",
    ],
    deps = [
        "@pip//jsonschema",
        "@pip//pytest",
    ],
)

js_test(
    name = "rehash_test",
    data = ["fixtures/rehash.mjs"],
    entry_point = "fixtures/rehash.test.mjs",
)

# Indexed through //:repo_docs so the factory does not modify the monolith.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//:__pkg__"],
)
