# gazelle:ignore

load("//projects/monolith:bdd_test.bzl", "bdd_test")

# Real-Postgres test: agents_writer can only read the agent cluster snapshot.
bdd_test(
    name = "agent_view_grants_test",
    srcs = ["agent_view_grants_test.py"],
)
