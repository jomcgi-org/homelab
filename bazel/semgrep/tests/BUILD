# gazelle:exclude **

package(default_visibility = ["//visibility:public"])

filegroup(
    name = "yaml_fixtures",
    srcs = glob(
        ["fixtures/*.yaml"],
        # Exclude Go-rule reference fixtures that are not tested against yaml_rules.
        # Exclude SQL/generic and kubernetes-specific fixtures that are tested separately.
        exclude = [
            "fixtures/claude-print-missing-permission-mode.yaml",
            "fixtures/fastapi-bare-tuple-return.yaml",
            "fixtures/logger-exc-info-non-boolean.yaml",
            "fixtures/no-create-extension-sql.yaml",
            "fixtures/no-deprecated-api-subdomain.yaml",
            "fixtures/no-discarded-json-marshal.yaml",
            "fixtures/no-session-in-to-thread.yaml",
            "fixtures/no-shared-preload-libraries-cnpg.yaml",
            "fixtures/no-unquoted-helm-range-args.yaml",
            "fixtures/python-shadow-module-import.yaml",
            "fixtures/require-cnpg-cluster-resources.yaml",
            "fixtures/require-component-label-in-selector.yaml",
            "fixtures/unsafe-json-field-access.yaml",
        ],
    ),
)

# Fixture for the stale-copyright-year rule (languages: generic).
# Referenced by //bazel/semgrep/rules:stale_copyright_year_test.
filegroup(
    name = "stale_copyright_year_fixtures",
    srcs = ["fixtures/stale-copyright-year.yaml"],
)

filegroup(
    name = "typescript_fixtures",
    srcs = glob(["fixtures/*.ts"]),
)

# Fixture for the no-create-event-dispatcher-runes rule (languages: generic, paths: projects/**/*.svelte).
# Referenced by //bazel/semgrep/rules:no_create_event_dispatcher_runes_test.
filegroup(
    name = "no_create_event_dispatcher_runes_fixtures",
    srcs = ["fixtures/no-create-event-dispatcher-runes.svelte"],
)

# Fixture for the no-stale-repo-paths rule (languages: generic).
# Referenced by //bazel/semgrep/rules:generic_rules_test.
filegroup(
    name = "no_stale_repo_paths_fixtures",
    srcs = ["fixtures/no-stale-repo-paths.md"],
)

# Fixture for the no-create-extension-sql rule (languages: generic, paths: **/migrations/*.sql).
# Referenced by //bazel/semgrep/rules:sql_rules_test.
filegroup(
    name = "no_create_extension_sql_fixtures",
    srcs = ["fixtures/no-create-extension-sql.yaml"],
)

# Fixture for the no-generic-test-filename rule (languages: generic, paths-filtered).
# Referenced by //bazel/semgrep/rules:no_generic_test_filename_test.
filegroup(
    name = "no_generic_test_filename_fixtures",
    srcs = ["fixtures/no-generic-test-filename.py"],
)

# Fixture for the no-unquoted-helm-range-args rule (languages: generic).
# Referenced by //bazel/semgrep/rules:no_unquoted_helm_range_args_test.
filegroup(
    name = "no_unquoted_helm_range_args_fixtures",
    srcs = ["fixtures/no-unquoted-helm-range-args.yaml"],
)

# Fixture for the require-component-label-in-selector rule (languages: generic).
# Referenced by //bazel/semgrep/rules:require_component_label_in_selector_test.
filegroup(
    name = "require_component_label_in_selector_fixtures",
    srcs = ["fixtures/require-component-label-in-selector.yaml"],
)

# Fixture for the claude-print-missing-permission-mode rule (languages: generic, paths: shell *.sh).
# Referenced by //bazel/semgrep/rules:claude_print_missing_permission_mode_test.
filegroup(
    name = "claude_print_missing_permission_mode_fixtures",
    srcs = ["fixtures/claude-print-missing-permission-mode.yaml"],
)

# Fixture for the no-session-in-to-thread rule (languages: python).
# Referenced by //bazel/semgrep/rules:no_session_in_to_thread_test.
filegroup(
    name = "no_session_in_to_thread_fixtures",
    srcs = ["fixtures/no-session-in-to-thread.yaml"],
)

# Fixtures for kubernetes rules (require-readiness-probe, require-resource-limits, etc.).
# Referenced by //bazel/semgrep/rules:kubernetes_rules_test.
# New rule fixtures should be added here as they are created.
filegroup(
    name = "kubernetes_yaml_fixtures",
    srcs = glob([
        "fixtures/no-hardcoded-image-digest.yaml",
        "fixtures/no-host-network.yaml",
        "fixtures/no-privileged.yaml",
        "fixtures/no-shared-preload-libraries-cnpg.yaml",
        "fixtures/pdb-minAvailable-blocks-eviction.yaml",
        "fixtures/require-cnpg-cluster-resources.yaml",
        "fixtures/require-fsgroup-with-pvc.yaml",
        "fixtures/require-readiness-probe.yaml",
        "fixtures/require-resource-limits.yaml",
        "fixtures/require-tmp-emptydir.yaml",
    ]),
)
