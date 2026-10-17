# gazelle:ignore
load("//projects/monolith:bdd_test.bzl", "bdd_test")

# Runtime-created transport tables must retire without dropping shared objects.
bdd_test(
    name = "retired_transport_migration_test",
    srcs = ["retired_transport_migration_test.py"],
)
