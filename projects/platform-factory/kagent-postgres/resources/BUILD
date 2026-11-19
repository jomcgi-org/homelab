# secret_init.py runs inside the kagent-pg-setup Job (setup.yaml mounts it
# from a ConfigMap); it is not a Bazel target.
# gazelle:exclude secret_init.py

exports_files(["claims_test.sql"])

filegroup(
    name = "claims_test_files",
    srcs = [
        "setup.psql",
        "setup.sql",
    ],
    visibility = ["//projects/factory/tests/itsaplan:__pkg__"],
)
