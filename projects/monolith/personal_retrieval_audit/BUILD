# gazelle:ignore

load("//projects/monolith:bdd_test.bzl", "bdd_test")

# Real-Postgres test: agents_writer can insert audit rows through the narrow
# column grant, cannot read or delete them, and insert-triggered retention runs
# atomically through the definer-rights function. Owned here rather than in
# projects/monolith/BUILD because the central BUILD ratchet forbids new
# gazelle:exclude directives there.
bdd_test(
    name = "personal_retrieval_audit_grants_test",
    srcs = ["personal_retrieval_audit_grants_test.py"],
)
