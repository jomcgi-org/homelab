load("@rules_shell//shell:sh_test.bzl", "sh_test")

package(default_visibility = ["//visibility:public"])

exports_files(glob(["*.sh"]))

sh_test(
    name = "prefer_bb_remote_test",
    srcs = ["prefer-bb-remote_test.sh"],
    data = [":prefer-bb-remote.sh"],
)

sh_test(
    name = "check_public_reader_grant_test",
    srcs = ["check-public-reader-grant_test.sh"],
    data = [":check-public-reader-grant.sh"],
)

sh_test(
    name = "check_large_migration_sql_test",
    srcs = ["check-large-migration-sql_test.sh"],
    data = [":check-large-migration-sql.sh"],
)

sh_test(
    name = "check_constant_change_test_grep_test",
    srcs = ["check-constant-change-test-grep_test.sh"],
    data = [":check-constant-change-test-grep.sh"],
)

sh_test(
    name = "check_chart_version_targetrevision_sync_test",
    srcs = ["check-chart-version-targetrevision-sync_test.sh"],
    data = [":check-chart-version-targetrevision-sync.sh"],
)

sh_test(
    name = "validate_hooks_executable_test",
    srcs = ["validate-hooks-executable_test.sh"],
    data = [":validate-hooks-executable.sh"],
)
