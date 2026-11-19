load("@aspect_rules_js//js:defs.bzl", "js_library", "js_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Hand-maintained snapshot contract, fixtures and their conformance test.
# gazelle:ignore

# rules_js copies cross-package sources through their owning js_library.
js_library(
    name = "web_schema",
    srcs = glob(["schema/**"]),
    visibility = [
        "//projects/jomcgi-site/data:__pkg__",
        "//projects/jomcgi-site/web:__pkg__",
    ],
)

js_library(
    name = "web_fixtures",
    srcs = glob(["fixtures/**/*.json"]),
    visibility = ["//projects/jomcgi-site/web:__pkg__"],
)

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

py_test(
    name = "publisher_test",
    srcs = [
        "publisher.py",
        "publisher_test.py",
        "session_history.py",
    ],
    data = ["@gitleaks_linux_amd64//:gitleaks"],
    env = {"GITLEAKS_BIN": "$(rootpath @gitleaks_linux_amd64//:gitleaks)"},
    deps = ["@pip//pytest"],
)

py_test(
    name = "delivery_stats_test",
    srcs = [
        "delivery_stats.py",
        "delivery_stats_test.py",
    ],
    deps = ["@pip//pytest"],
)
