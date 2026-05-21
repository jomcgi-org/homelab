load("@rules_shell//shell:sh_test.bzl", "sh_test")

package(default_visibility = ["//visibility:public"])

exports_files(glob(["*.sh"]))

sh_test(
    name = "prefer_bb_remote_test",
    srcs = ["prefer-bb-remote_test.sh"],
    data = [":prefer-bb-remote.sh"],
)

sh_test(
    name = "check_python_file_shadows_package_test",
    srcs = ["check-python-file-shadows-package_test.sh"],
    data = [":check-python-file-shadows-package.sh"],
)

sh_test(
    name = "check_bdd_shared_testing_dep_test",
    srcs = ["check-bdd-shared-testing-dep_test.sh"],
    data = [":check-bdd-shared-testing-dep.sh"],
)

sh_test(
    name = "check_deployment_component_label_test",
    srcs = ["check-deployment-component-label_test.sh"],
    data = [":check-deployment-component-label.sh"],
)

sh_test(
    name = "check_migration_timestamp_order_test",
    srcs = ["check-migration-timestamp-order_test.sh"],
    data = [":check-migration-timestamp-order.sh"],
)

sh_test(
    name = "check_constant_change_test_grep_test",
    srcs = ["check-constant-change-test-grep_test.sh"],
    data = [":check-constant-change-test-grep.sh"],
)

sh_test(
    name = "check_public_route_visibility_filter_test",
    srcs = ["check-public-route-visibility-filter_test.sh"],
    data = [":check-public-route-visibility-filter.sh"],
)

sh_test(
    name = "check_semgrep_rule_has_fixture_test",
    srcs = ["check-semgrep-rule-has-fixture_test.sh"],
    data = [":check-semgrep-rule-has-fixture.sh"],
)

sh_test(
    name = "check_semgrep_helm_fixture_has_template_syntax_test",
    srcs = ["check-semgrep-helm-fixture-has-template-syntax_test.sh"],
    data = [":check-semgrep-helm-fixture-has-template-syntax.sh"],
)
